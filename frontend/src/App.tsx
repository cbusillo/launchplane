import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Command,
  Database,
  Eye,
  GitCompareArrows,
  KeyRound,
  LogOut,
  Loader2,
  Moon,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sun,
  TerminalSquare,
  XCircle
} from "lucide-react";
import { ReactNode, useEffect, useMemo, useState } from "react";
import { LaunchplaneApiError, listDrivers, logout, readAuthSession, readDriverView } from "./api";
import type {
  AuthIdentity,
  DriverActionDescriptor,
  DriverContextView,
  DriverDescriptor,
  DriverView,
  LaneSummary,
  PreviewSummary,
  Safety,
  Status
} from "./types";

type Theme = "dark" | "light";
type AuthStatus = "checking" | "signed_out" | "signed_in";
type DriverChoice = {
  driverId: string;
  context: string;
  label: string;
};
type PromotionVerdict = "ready" | "pending" | "blocked";
type PromotionGate = {
  label: string;
  status: Status | string;
  detail: string;
  evidence: string;
};
type PromotionDecision = {
  verdict: PromotionVerdict;
  gates: PromotionGate[];
  latestEvidence: string;
  blockingEvidence: string;
  prodArtifact: string;
  testingArtifact: string;
};
type EvidenceRow = {
  id: string;
  title: string;
  detail: string;
  status: string;
  time: string;
  lane: string;
  kind: string;
  facts: EvidenceFact[];
};
type EvidenceFact = {
  label: string;
  value: string;
  mono?: boolean;
  status?: Status | string;
};

const THEME_STORAGE_KEY = "launchplane.theme";
const DEFAULT_CHOICES: DriverChoice[] = [
  { driverId: "verireel", context: "verireel", label: "VeriReel" },
  { driverId: "odoo", context: "opw", label: "Odoo" }
];
const FIXTURE_ACTIONS: DriverActionDescriptor[] = [
  {
    action_id: "prod_backup_gate",
    label: "Capture prod backup gate",
    description: "Capture concrete backup evidence before a prod-changing action.",
    safety: "safe_write",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/verireel/prod-backup-gate",
    writes_records: ["backup_gate"]
  },
  {
    action_id: "prod_promotion",
    label: "Promote testing to prod",
    description: "Promote a stored testing artifact to prod after backup-gate evidence passes.",
    safety: "mutation",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/verireel/prod-promotion",
    writes_records: ["deployment", "promotion", "inventory", "release_tuple"]
  }
];
const FIXTURE_VERIREEL_DRIVER: DriverDescriptor = {
  schema_version: 1,
  driver_id: "verireel",
  label: "VeriReel",
  product: "verireel",
  description: "Fixture VeriReel driver.",
  context_patterns: ["verireel"],
  provider_boundary: "Launchplane evidence only.",
  capabilities: [
    {
      capability_id: "preview_lifecycle",
      label: "Preview lifecycle",
      description: "Preview lifecycle fixture.",
      actions: [],
      panels: ["preview_inventory"]
    }
  ],
  actions: FIXTURE_ACTIONS,
  setting_groups: []
};
const FIXTURE_ODOO_DRIVER: DriverDescriptor = {
  ...FIXTURE_VERIREEL_DRIVER,
  driver_id: "odoo",
  label: "Odoo",
  product: "odoo",
  capabilities: [],
  actions: []
};

export function App() {
  const showFixtureGallery = import.meta.env.DEV && new URLSearchParams(window.location.search).get("fixtures") === "1";
  const [theme, setTheme] = useState<Theme>(() => {
    return window.sessionStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
  });
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [drivers, setDrivers] = useState<DriverDescriptor[]>([]);
  const [selected, setSelected] = useState<DriverChoice>(DEFAULT_CHOICES[0]);
  const [prodView, setProdView] = useState<DriverContextView | null>(null);
  const [testingView, setTestingView] = useState<DriverContextView | null>(null);
  const [previewView, setPreviewView] = useState<DriverContextView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");
  const [traceId, setTraceId] = useState<string>("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [reviewAction, setReviewAction] = useState<DriverActionDescriptor | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceRow | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.sessionStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    let active = true;
    setAuthStatus("checking");
    readAuthSession()
      .then((payload) => {
        if (!active) {
          return;
        }
        setIdentity(payload.identity);
        setAuthStatus("signed_in");
      })
      .catch((apiError: unknown) => {
        if (!active) {
          return;
        }
        setIdentity(null);
        setAuthStatus("signed_out");
        if (apiError instanceof LaunchplaneApiError && apiError.statusCode !== 401) {
          setError(apiError.message);
          setTraceId(apiError.traceId);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (authStatus !== "signed_in") {
      setDrivers([]);
      setProdView(null);
      setTestingView(null);
      setPreviewView(null);
      setError("");
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError("");
    setTraceId("");
    Promise.all([
      listDrivers(),
      readDriverView(selected.context, "prod"),
      readDriverView(selected.context, "testing"),
      selected.driverId === "verireel"
        ? readDriverView("verireel-testing", "")
        : Promise.resolve(null)
    ])
      .then(([driverPayload, prodPayload, testingPayload, previewPayload]) => {
        if (controller.signal.aborted) {
          return;
        }
        setDrivers(driverPayload.drivers);
        setProdView(prodPayload.view);
        setTestingView(testingPayload.view);
        setPreviewView(previewPayload?.view ?? null);
      })
      .catch((apiError: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (apiError instanceof LaunchplaneApiError) {
          setError(apiError.message);
          setTraceId(apiError.traceId);
        } else if (apiError instanceof Error) {
          setError(apiError.message);
        } else {
          setError("Launchplane API request failed.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [authStatus, selected, refreshKey]);

  const choices = useMemo(() => {
    if (!drivers.length) {
      return DEFAULT_CHOICES;
    }
    return drivers.flatMap((driver) => {
      const stableContexts = driver.context_patterns.filter((context) => {
        return context !== "verireel-testing" && !context.includes("*");
      });
      return stableContexts.map((context) => ({
        driverId: driver.driver_id,
        context,
        label: driver.label
      }));
    });
  }, [drivers]);

  const currentDriver = drivers.find((driver) => driver.driver_id === selected.driverId);
  const prodDriverView = findDriverView(prodView, selected.driverId);
  const testingDriverView = findDriverView(testingView, selected.driverId);
  const previewDriverView = findDriverView(previewView, selected.driverId);

  const actions = useMemo(() => {
    return currentDriver?.actions ?? prodDriverView?.available_actions ?? testingDriverView?.available_actions ?? [];
  }, [currentDriver, prodDriverView, testingDriverView]);
  const promotionDecision = useMemo(
    () => buildPromotionDecision(prodDriverView?.lane_summary ?? null, testingDriverView?.lane_summary ?? null),
    [prodDriverView, testingDriverView]
  );
  const nextAction = pickNextAction(actions, promotionDecision.verdict);

  function signOut() {
    logout().finally(() => {
      setIdentity(null);
      setAuthStatus("signed_out");
      setDrivers([]);
      setProdView(null);
      setTestingView(null);
      setPreviewView(null);
    });
  }

  return (
    <div className="app-shell">
      <Header
        choices={choices}
        selected={selected}
        onSelect={setSelected}
        theme={theme}
        onThemeChange={setTheme}
        loading={loading}
        identity={identity}
        onLogout={signOut}
        onRefresh={() => setRefreshKey((value) => value + 1)}
      />
      <main className="operator-main">
        {authStatus !== "signed_in" ? (
          showFixtureGallery ? (
            <StateFixtureGallery actions={FIXTURE_ACTIONS} />
          ) : (
            <AuthPanel checking={authStatus === "checking"} />
          )
        ) : (
          <>
            {error ? <ApiErrorPanel message={error} traceId={traceId} onClearToken={signOut} /> : null}
            <section className="lane-grid" aria-busy={loading}>
              <LanePanel
                title="Prod"
                laneKind="prod"
                lane={prodDriverView?.lane_summary ?? null}
                loading={loading}
              />
              <PromotionBridge
                prod={prodDriverView?.lane_summary ?? null}
                testing={testingDriverView?.lane_summary ?? null}
                actions={actions}
                decision={promotionDecision}
                loading={loading}
                onAction={setReviewAction}
              />
              <LanePanel
                title="Testing"
                laneKind="testing"
                lane={testingDriverView?.lane_summary ?? null}
                loading={loading}
              />
            </section>
            <section className="work-grid">
              <PreviewInventory
                driver={currentDriver ?? null}
                previews={previewDriverView?.preview_summaries ?? []}
                loading={loading}
              />
              <ActionList actions={actions} nextAction={nextAction} loading={loading} onAction={setReviewAction} />
            </section>
            <section className="work-grid work-grid-evidence">
              <SecretBindingList driver={currentDriver ?? null} lane={prodDriverView?.lane_summary ?? null} />
              <EvidenceTimeline
                prod={prodDriverView?.lane_summary ?? null}
                testing={testingDriverView?.lane_summary ?? null}
                previews={previewDriverView?.preview_summaries ?? []}
                onSelect={setSelectedEvidence}
              />
            </section>
          </>
        )}
      </main>
      <ActionReviewDialog action={reviewAction} onClose={() => setReviewAction(null)} />
      <EvidenceDetailDrawer evidence={selectedEvidence} onClose={() => setSelectedEvidence(null)} />
    </div>
  );
}

function Header({
  choices,
  selected,
  onSelect,
  theme,
  onThemeChange,
  loading,
  identity,
  onLogout,
  onRefresh
}: {
  choices: DriverChoice[];
  selected: DriverChoice;
  onSelect: (choice: DriverChoice) => void;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  loading: boolean;
  identity: AuthIdentity | null;
  onLogout: () => void;
  onRefresh: () => void;
}) {
  const selectedValue = `${selected.driverId}:${selected.context}`;
  const selectId = "launchplane-context-select";

  return (
    <header className="topbar">
      <div className="brand-block">
        <div className="brand-mark" aria-hidden="true">
          LP
        </div>
        <div>
          <div className="brand-title">Launchplane</div>
          <div className="brand-meta">
            <span>{selected.context}</span>
            <span>{selected.driverId}</span>
          </div>
        </div>
      </div>
      <div className="topbar-controls">
        {identity ? (
          <div className="operator-chip" title={identity.login}>
            <span>{identity.login}</span>
            <strong>{identity.role === "admin" ? "Admin" : "Read only"}</strong>
          </div>
        ) : null}
        <label className="select-label">
          <span>Context</span>
          <select
            id={selectId}
            aria-label="Launchplane context"
            value={selectedValue}
            onChange={(event) => {
              const choice = choices.find((item) => `${item.driverId}:${item.context}` === event.target.value);
              if (choice) {
                onSelect(choice);
              }
            }}
          >
            {choices.map((choice) => (
              <option key={`${choice.driverId}:${choice.context}`} value={`${choice.driverId}:${choice.context}`}>
                {choice.label} / {choice.context}
              </option>
            ))}
          </select>
          <ChevronDown size={14} aria-hidden="true" />
        </label>
        <button
          className="icon-button"
          type="button"
          title="Refresh"
          aria-label={loading ? "Refreshing Launchplane view" : "Refresh Launchplane view"}
          onClick={onRefresh}
        >
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
        </button>
        {identity ? (
          <button
            className="icon-button"
            type="button"
            title="Sign out"
            aria-label="Sign out of Launchplane"
            onClick={onLogout}
          >
            <LogOut size={16} />
          </button>
        ) : null}
        <button
          className="icon-button"
          type="button"
          title={theme === "dark" ? "Use light mode" : "Use dark mode"}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          onClick={() => onThemeChange(theme === "dark" ? "light" : "dark")}
        >
          {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
        </button>
        <button
          className="command-button"
          type="button"
          title="Command palette unavailable"
          aria-label="Command palette unavailable in this read-only view"
          disabled
        >
          <Command size={15} />
          <span>Command</span>
        </button>
      </div>
    </header>
  );
}

function AuthPanel({ checking }: { checking: boolean }) {
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
        <a className="button button-primary" href={loginHref} aria-disabled={checking}>
          {checking ? <Loader2 className="spin" size={15} /> : <KeyRound size={15} />}
          <span>{checking ? "Checking session" : "Sign in with GitHub"}</span>
        </a>
      </div>
    </section>
  );
}

function ApiErrorPanel({
  message,
  traceId,
  onClearToken
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

function LanePanel({
  title,
  laneKind,
  lane,
  loading
}: {
  title: string;
  laneKind: "prod" | "testing";
  lane: LaneSummary | null;
  loading: boolean;
}) {
  const artifact = artifactFromLane(lane);
  const deployStatus = lane?.latest_deployment?.deploy.status ?? lane?.inventory?.deploy.status ?? "unknown";
  const healthStatus = lane?.inventory?.destination_health.status ?? lane?.latest_deployment?.destination_health.status ?? "unknown";
  const backupStatus = lane?.latest_backup_gate?.status ?? "unknown";
  const settingsStatus = lane?.odoo_instance_override ? "pass" : "unknown";
  const updatedAt = lane?.inventory?.updated_at ?? lane?.latest_deployment?.deploy.finished_at ?? "";
  const targetName = lane?.latest_deployment?.deploy.target_name ?? lane?.inventory?.deploy.target_name ?? "";
  const releaseIdentity = releaseIdentityFromLane(lane);

  return (
    <section className={`panel lane-panel lane-${laneKind}`}>
      <PanelHead eyebrow="environment lane" title={title} right={<StatusPill status={worstStatus([deployStatus, healthStatus])} />} />
      {loading ? (
        <SkeletonRows />
      ) : (
        <div className="lane-body">
          <div className="lane-release">
            <span className={`lane-chip lane-chip-${laneKind}`}>{laneKind}</span>
            <code>{releaseIdentity || "release unknown"}</code>
          </div>
          <div className="lane-metrics">
            <MetricTile label="Deploy" status={deployStatus} value={labelForStatus(deployStatus)} />
            <MetricTile label="Health" status={healthStatus} value={labelForStatus(healthStatus)} />
            <MetricTile label="Backup" status={backupStatus} value={labelForStatus(backupStatus)} />
            <MetricTile label="Settings" status={settingsStatus} value={labelForStatus(settingsStatus)} />
          </div>
          <KeyValue label="Artifact" value={artifact} mono muted={!artifact} />
          <KeyValue label="Target" value={targetName} mono muted={!targetName} />
          <KeyValue label="Source" value={shorten(lane?.inventory?.source_git_ref ?? lane?.latest_deployment?.source_git_ref ?? "")} mono />
          <KeyValue label="Deployment" value={lane?.latest_deployment?.record_id ?? lane?.inventory?.deployment_record_id ?? ""} mono />
          <KeyValue label="Promotion" value={lane?.latest_promotion?.record_id ?? lane?.inventory?.promotion_record_id ?? ""} mono muted={!lane?.latest_promotion && !lane?.inventory?.promotion_record_id} />
          <KeyValue label="Updated" value={formatTime(updatedAt)} mono />
          <EvidenceStrip lane={lane} laneKind={laneKind} />
        </div>
      )}
    </section>
  );
}

function PromotionBridge({
  prod,
  testing,
  actions,
  decision,
  loading,
  onAction
}: {
  prod: LaneSummary | null;
  testing: LaneSummary | null;
  actions: DriverActionDescriptor[];
  decision: PromotionDecision;
  loading: boolean;
  onAction: (action: DriverActionDescriptor) => void;
}) {
  const primaryAction = pickNextAction(actions, decision.verdict);
  const verdictLabel = decision.verdict === "ready"
    ? "Ready to promote"
    : decision.verdict === "blocked"
      ? "Promotion blocked"
      : "Evidence pending";

  return (
    <section className={`panel promotion-bridge verdict-${decision.verdict}`}>
      <PanelHead eyebrow="promotion decision" title="Testing to prod" right={<StatusPill status={decision.verdict} />} />
      {loading ? (
        <SkeletonRows />
      ) : (
        <>
          <div className="bridge-verdict">
            <span>{verdictLabel}</span>
            <strong>{decision.blockingEvidence || decision.latestEvidence}</strong>
          </div>
          <div className="bridge-direction" aria-label="Promotion artifact delta">
            <div>
              <span className="lane-chip lane-chip-testing">testing</span>
              <code>{decision.testingArtifact || "unknown candidate"}</code>
            </div>
            <GitCompareArrows size={20} aria-hidden="true" />
            <div>
              <span className="lane-chip lane-chip-prod">prod</span>
              <code>{decision.prodArtifact || "unknown prod"}</code>
            </div>
          </div>
          <div className="gate-list">
            {decision.gates.map((gate) => (
              <div className="gate-row" key={gate.label}>
                <StatusIcon status={gate.status} />
                <span>
                  {gate.label}
                  <em>{gate.evidence}</em>
                </span>
                <strong data-status={gate.status}>{gate.detail}</strong>
              </div>
            ))}
          </div>
          {primaryAction ? (
            <button
              className="button button-primary bridge-action"
              type="button"
              data-safety={primaryAction.safety}
              aria-label={`Review ${primaryAction.label}`}
              disabled={decision.verdict !== "ready" && primaryAction.safety === "mutation"}
              onClick={() => onAction(primaryAction)}
            >
              <TerminalSquare size={16} />
              <span>{primaryAction.label}</span>
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function PreviewInventory({
  driver,
  previews,
  loading
}: {
  driver: DriverDescriptor | null;
  previews: PreviewSummary[];
  loading: boolean;
}) {
  const exposesPreviews = Boolean(
    driver?.capabilities.some((capability) => capability.capability_id === "preview_lifecycle")
  );
  const latestPreview = previews
    .slice()
    .sort((left, right) => {
      const leftTime = left.latest_generation?.finished_at ?? left.preview.updated_at;
      const rightTime = right.latest_generation?.finished_at ?? right.preview.updated_at;
      return rightTime.localeCompare(leftTime);
    })
    .at(0);

  return (
    <section className="panel preview-panel">
      <PanelHead
        eyebrow="preview lane"
        title={exposesPreviews ? "Preview inventory" : "Previews not exposed"}
        right={exposesPreviews ? <span className="count-chip">{previews.length} active</span> : null}
      />
      {loading ? (
        <SkeletonRows />
      ) : !exposesPreviews ? (
        <StateBlock icon={<Eye size={18} />} title="Driver does not expose preview lifecycle" />
      ) : previews.length === 0 ? (
        <StateBlock icon={<Archive size={18} />} title="No active previews" />
      ) : (
        <div className="preview-list">
          {previews.map((summary) => {
            const health = summary.latest_generation?.overall_health_status ?? "unknown";
            return (
              <article className="preview-row" key={summary.preview.preview_id}>
                <div>
                  <strong>{summary.preview.preview_label}</strong>
                  <a href={summary.preview.anchor_pr_url}>{summary.preview.anchor_repo}#{summary.preview.anchor_pr_number}</a>
                </div>
                <code>{summary.latest_generation?.artifact_id ?? summary.preview.preview_id}</code>
                <StatusPill status={health} />
              </article>
            );
          })}
        </div>
      )}
      <div className="preview-footer">
        <KeyValue
          label="Capability"
          value={exposesPreviews ? "preview_lifecycle" : "not exposed by driver"}
          mono
          status={exposesPreviews ? "pass" : "unknown"}
        />
        <KeyValue
          label="Latest"
          value={latestPreview ? formatTime(latestPreview.latest_generation?.finished_at ?? latestPreview.preview.updated_at) : "unknown"}
          mono
          muted={!latestPreview}
        />
      </div>
    </section>
  );
}

function ActionList({
  actions,
  nextAction,
  loading,
  onAction
}: {
  actions: DriverActionDescriptor[];
  nextAction?: DriverActionDescriptor;
  loading: boolean;
  onAction: (action: DriverActionDescriptor) => void;
}) {
  const groups: Safety[] = ["read", "safe_write", "mutation", "destructive"];
  return (
    <section className="panel">
      <PanelHead eyebrow="next safe action" title="Actions" />
      {loading ? (
        <SkeletonRows />
      ) : (
        <div className="action-groups">
          {nextAction ? (
            <button
              className="next-action-card"
              data-safety={nextAction.safety}
              type="button"
              aria-label={`Review next safe action: ${nextAction.label}`}
              onClick={() => onAction(nextAction)}
            >
              <span>
                <SafetyIcon safety={nextAction.safety} />
                <strong>{nextAction.label}</strong>
              </span>
              <em>{nextAction.description}</em>
              <code>{nextAction.method} {nextAction.route_path}</code>
            </button>
          ) : null}
          {groups.map((safety) => {
            const groupedActions = actions.filter((action) => action.safety === safety);
            if (!groupedActions.length) {
              return null;
            }
            return (
              <div className="action-group" key={safety}>
                <div className="action-group-title">
                  <SafetyIcon safety={safety} />
                  <span>{safetyLabel(safety)}</span>
                </div>
                {groupedActions.map((action) => (
                  <button
                    className="action-row"
                    data-safety={action.safety}
                    type="button"
                    key={action.action_id}
                    aria-label={`Review ${action.label}, ${safetyLabel(action.safety)}`}
                    onClick={() => onAction(action)}
                  >
                    <span>{action.label}</span>
                    <code>{action.scope}</code>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function SecretBindingList({ driver, lane }: { driver: DriverDescriptor | null; lane: LaneSummary | null }) {
  const bindingHints = driver?.setting_groups.flatMap((group) => {
    return group.secret_bindings.map((binding) => ({ group, binding }));
  }) ?? [];
  const actualBindings = lane?.secret_bindings ?? [];

  return (
    <section className="panel">
      <PanelHead eyebrow="managed secrets" title="Bindings" right={<KeyRound size={17} aria-hidden="true" />} />
      {actualBindings.length ? (
        <div className="secret-list">
          {actualBindings.map((binding) => (
            <div className="secret-row" key={binding.binding_id}>
              <span className="lane-chip lane-chip-prod">{binding.instance || binding.context || "global"}</span>
              <strong>{binding.binding_key}</strong>
              <span>{binding.integration}</span>
              <StatusPill status={binding.status === "configured" ? "pass" : "blocked"} />
            </div>
          ))}
        </div>
      ) : (
        <div className="secret-list">
          {bindingHints.map(({ group, binding }) => (
            <div className="secret-row" key={`${group.group_id}:${binding}`}>
              <span className="lane-chip lane-chip-prod">{group.scope}</span>
              <strong>{binding}</strong>
              <span>{group.label}</span>
              <StatusPill status="unknown" />
            </div>
          ))}
          {!bindingHints.length ? <StateBlock icon={<KeyRound size={18} />} title="No binding metadata" /> : null}
        </div>
      )}
    </section>
  );
}

function EvidenceTimeline({
  prod,
  testing,
  previews,
  onSelect
}: {
  prod: LaneSummary | null;
  testing: LaneSummary | null;
  previews: PreviewSummary[];
  onSelect: (row: EvidenceRow) => void;
}) {
  const rows = buildEvidenceRows(prod, testing, previews);
  return (
    <section className="panel">
      <PanelHead eyebrow="evidence" title="Timeline" right={<Database size={17} aria-hidden="true" />} />
      <div className="evidence-list">
        {rows.length ? (
          rows.map((row) => (
            <button
              className="evidence-row"
              key={row.id}
              type="button"
              aria-label={`Inspect ${row.title} evidence`}
              onClick={() => onSelect(row)}
            >
              <StatusIcon status={row.status} />
              <div>
                <strong>
                  <span className={`lane-chip lane-chip-${row.lane}`}>{row.lane}</span>
                  {row.title}
                </strong>
                <span>{row.detail}</span>
              </div>
              <code>
                {row.kind}
                <br />
                {formatTime(row.time)}
              </code>
            </button>
          ))
        ) : (
          <StateBlock icon={<Clock3 size={18} />} title="No evidence records" />
        )}
      </div>
    </section>
  );
}

function EvidenceDetailDrawer({ evidence, onClose }: { evidence: EvidenceRow | null; onClose: () => void }) {
  useEffect(() => {
    if (!evidence) {
      return undefined;
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [evidence, onClose]);

  if (!evidence) {
    return null;
  }

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        className="evidence-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evidence-drawer-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="drawer-header">
          <div>
            <p className="eyebrow">evidence detail</p>
            <h2 id="evidence-drawer-title">{evidence.title}</h2>
          </div>
          <button className="icon-button" type="button" aria-label="Close evidence detail" onClick={onClose}>
            <XCircle size={15} aria-hidden="true" />
          </button>
        </div>
        <div className="drawer-meta">
          <span className={`lane-chip lane-chip-${evidence.lane}`}>{evidence.lane}</span>
          <StatusPill status={evidence.status} />
          <code>{evidence.kind}</code>
          <code>{formatTime(evidence.time)}</code>
        </div>
        <p className="drawer-summary">{evidence.detail || "No detail recorded for this evidence row."}</p>
        <div className="drawer-facts">
          {evidence.facts.map((fact) => (
            <KeyValue
              key={`${fact.label}:${fact.value}`}
              label={fact.label}
              value={fact.value}
              mono={fact.mono}
              status={fact.status}
            />
          ))}
        </div>
      </aside>
    </div>
  );
}

function StateFixtureGallery({ actions }: { actions: DriverActionDescriptor[] }) {
  const readyProd = fixtureLane({ instance: "prod", artifact: "ghcr.io/every/verireel@sha256:11112222", deployStatus: "pass", healthStatus: "pass", backupStatus: "pass" });
  const readyTesting = fixtureLane({ instance: "testing", artifact: "ghcr.io/every/verireel@sha256:aa55cc77", deployStatus: "pass", healthStatus: "pass" });
  const missingBackupProd = fixtureLane({ instance: "prod", artifact: "ghcr.io/every/verireel@sha256:11112222", deployStatus: "pass", healthStatus: "pass" });
  const failedTesting = fixtureLane({ instance: "testing", artifact: "ghcr.io/every/verireel@sha256:fail0001", deployStatus: "fail", healthStatus: "fail" });

  return (
    <section className="fixture-gallery">
      <PanelHead eyebrow="development fixtures" title="Operator state coverage" />
      <div className="fixture-grid">
        <div className="fixture-card">
          <PromotionBridge prod={missingBackupProd} testing={readyTesting} actions={actions} decision={buildPromotionDecision(missingBackupProd, readyTesting)} loading={false} onAction={() => undefined} />
        </div>
        <div className="fixture-card">
          <PromotionBridge prod={readyProd} testing={failedTesting} actions={actions} decision={buildPromotionDecision(readyProd, failedTesting)} loading={false} onAction={() => undefined} />
        </div>
        <div className="fixture-card">
          <LanePanel title="Failed testing lane" laneKind="testing" lane={failedTesting} loading={false} />
        </div>
        <div className="fixture-card">
          <PreviewInventory driver={FIXTURE_VERIREEL_DRIVER} previews={[]} loading={false} />
        </div>
        <div className="fixture-card">
          <PreviewInventory driver={FIXTURE_ODOO_DRIVER} previews={[]} loading={false} />
        </div>
      </div>
    </section>
  );
}

function ActionReviewDialog({
  action,
  onClose
}: {
  action: DriverActionDescriptor | null;
  onClose: () => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [operatorReason, setOperatorReason] = useState("");
  const requiresTypedConfirmation = action?.safety === "destructive" || action?.safety === "mutation";
  const requiresReason = action?.safety === "destructive";
  const confirmationPhrase = action ? `confirm ${action.action_id}` : "";
  const canConfirm = !requiresTypedConfirmation || (
    confirmation === confirmationPhrase && (!requiresReason || operatorReason.trim().length > 0)
  );

  useEffect(() => {
    setConfirmation("");
    setOperatorReason("");
  }, [action]);

  if (!action) {
    return null;
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="action-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <PanelHead eyebrow={safetyLabel(action.safety)} title={action.label} right={<SafetyIcon safety={action.safety} />} />
        <p>{action.description}</p>
        {requiresTypedConfirmation ? (
          <div className="risk-callout" data-safety={action.safety}>
            <SafetyIcon safety={action.safety} />
            <div>
              <strong>{action.safety === "destructive" ? "Destructive operator review" : "Mutation review"}</strong>
              <span>
                This prepares a non-executing request preview for an action that can change Launchplane records.
              </span>
            </div>
          </div>
        ) : null}
        <div className="dialog-facts">
          <KeyValue label="Action" value={action.action_id} mono />
          <KeyValue label="Safety" value={safetyLabel(action.safety)} status={action.safety === "destructive" ? "blocked" : "unknown"} />
          <KeyValue label="Route" value={`${action.method} ${action.route_path}`} mono />
          <KeyValue label="Scope" value={action.scope} />
          <KeyValue label="Writes" value={action.writes_records.join(", ") || "none"} mono />
        </div>
        {requiresTypedConfirmation ? (
          <div className="confirm-stack">
            {requiresReason ? (
              <label className="confirm-label">
                <span>Operator reason</span>
                <textarea
                  value={operatorReason}
                  onChange={(event) => setOperatorReason(event.target.value)}
                  rows={3}
                  spellCheck={false}
                />
              </label>
            ) : null}
            <label className="confirm-label">
              <span>Type "{confirmationPhrase}"</span>
              <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false} />
            </label>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button className="button" type="button" aria-label="Cancel action review" onClick={onClose}>
            Cancel
          </button>
          <button
            className="button button-primary"
            data-safety={action.safety}
            type="button"
            aria-label={`Prepare ${action.label} request`}
            disabled={!canConfirm}
          >
            Prepare request
          </button>
        </div>
      </section>
    </div>
  );
}

function PanelHead({ eyebrow, title, right }: { eyebrow: string; title: string; right?: ReactNode }) {
  return (
    <div className="panel-head">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
      </div>
      {right ? <div className="panel-right">{right}</div> : null}
    </div>
  );
}

function KeyValue({
  label,
  value,
  mono = false,
  muted = false,
  status
}: {
  label: string;
  value: string;
  mono?: boolean;
  muted?: boolean;
  status?: Status | string;
}) {
  return (
    <div className="kv-row">
      <span>{label}</span>
      <strong className={`${mono ? "mono" : ""} ${muted ? "muted" : ""}`} data-status={status}>
        {value || "unknown"}
      </strong>
    </div>
  );
}

function MetricTile({ label, status, value }: { label: string; status: Status | string; value: string }) {
  return (
    <div className="metric-tile" data-status={status}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function StatusPill({ status }: { status: Status | string }) {
  return (
    <span className="status-pill" data-status={status}>
      <StatusIcon status={status} />
      {labelForStatus(status)}
    </span>
  );
}

function StatusIcon({ status }: { status: Status | string }) {
  if (status === "pass" || status === "ready") {
    return <CheckCircle2 size={15} aria-hidden="true" />;
  }
  if (status === "fail" || status === "blocked") {
    return <XCircle size={15} aria-hidden="true" />;
  }
  if (status === "pending") {
    return <Clock3 size={15} aria-hidden="true" />;
  }
  return <AlertTriangle size={15} aria-hidden="true" />;
}

function SafetyIcon({ safety }: { safety: Safety }) {
  if (safety === "destructive") {
    return <ShieldAlert size={15} aria-hidden="true" />;
  }
  if (safety === "mutation") {
    return <GitCompareArrows size={15} aria-hidden="true" />;
  }
  if (safety === "safe_write") {
    return <ShieldCheck size={15} aria-hidden="true" />;
  }
  return <Eye size={15} aria-hidden="true" />;
}

function StateBlock({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div className="state-block">
      {icon}
      <strong>{title}</strong>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="skeleton-list" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </div>
  );
}

function EvidenceStrip({ lane, laneKind }: { lane: LaneSummary | null; laneKind: "prod" | "testing" }) {
  const backup = lane?.latest_backup_gate?.status ?? "unknown";
  const promotion = lane?.latest_promotion?.deploy.status ?? "unknown";
  return (
    <div className="evidence-strip">
      <span className={`lane-chip lane-chip-${laneKind}`}>{laneKind}</span>
      <StatusPill status={backup} />
      <StatusPill status={promotion} />
    </div>
  );
}

function buildPromotionDecision(prod: LaneSummary | null, testing: LaneSummary | null): PromotionDecision {
  const gates = promotionGates(prod, testing);
  const verdict = gates.some((gate) => gate.status === "fail" || gate.status === "blocked")
    ? "blocked"
    : gates.some((gate) => gate.status === "pending" || gate.status === "unknown")
      ? "pending"
      : "ready";
  const blockingGate = gates.find((gate) => gate.status === "fail" || gate.status === "blocked" || gate.status === "unknown");
  return {
    verdict,
    gates,
    latestEvidence: latestEvidenceLabel(prod, testing),
    blockingEvidence: blockingGate ? `${blockingGate.label}: ${blockingGate.evidence}` : "",
    prodArtifact: artifactFromLane(prod),
    testingArtifact: artifactFromLane(testing)
  };
}

function promotionGates(prod: LaneSummary | null, testing: LaneSummary | null): PromotionGate[] {
  const testingDeploy = testing?.latest_deployment?.deploy.status ?? testing?.inventory?.deploy.status ?? "unknown";
  const testingHealth = testing?.inventory?.destination_health.status ?? testing?.latest_deployment?.destination_health.status ?? "unknown";
  const backupGate = prod?.latest_backup_gate?.status ?? "unknown";
  const candidateArtifact = artifactFromLane(testing);
  const prodArtifact = artifactFromLane(prod);
  return [
    {
      label: "Candidate deployment",
      status: normalizeGateStatus(testingDeploy),
      detail: labelForStatus(testingDeploy),
      evidence: testing?.latest_deployment?.record_id ?? "missing deployment evidence"
    },
    {
      label: "Candidate health",
      status: normalizeGateStatus(testingHealth),
      detail: labelForStatus(testingHealth),
      evidence: testing?.inventory?.destination_health.verified ? "verified healthcheck" : "missing healthcheck"
    },
    {
      label: "Prod backup gate",
      status: backupGate === "pass" ? "pass" : backupGate === "fail" ? "blocked" : "unknown",
      detail: labelForStatus(backupGate),
      evidence: prod?.latest_backup_gate?.record_id ?? "required before prod change"
    },
    {
      label: "Artifact delta",
      status: candidateArtifact && candidateArtifact !== prodArtifact ? "pass" : "unknown",
      detail: candidateArtifact && prodArtifact ? "changed" : "missing",
      evidence: candidateArtifact && prodArtifact ? `${shorten(candidateArtifact)} -> ${shorten(prodArtifact)}` : "candidate or prod artifact missing"
    }
  ];
}

function normalizeGateStatus(status: Status | string): Status {
  if (status === "pass") {
    return "pass";
  }
  if (status === "fail") {
    return "blocked";
  }
  if (status === "pending") {
    return "pending";
  }
  return "unknown";
}

function buildEvidenceRows(prod: LaneSummary | null, testing: LaneSummary | null, previews: PreviewSummary[]): EvidenceRow[] {
  const rows: EvidenceRow[] = [];
  [
    { lane: prod, laneName: "prod" },
    { lane: testing, laneName: "testing" }
  ].forEach(({ lane, laneName }) => {
    if (lane?.latest_deployment) {
      const deployment = lane.latest_deployment;
      rows.push({
        id: deployment.record_id,
        title: `${lane.instance} deployment`,
        detail: artifactFromLane(lane) || deployment.record_id,
        status: deployment.deploy.status,
        time: deployment.deploy.finished_at ?? lane.inventory?.updated_at ?? "",
        lane: laneName,
        kind: "deploy",
        facts: [
          { label: "Record", value: deployment.record_id, mono: true },
          { label: "Artifact", value: artifactFromLane(lane) || "unknown", mono: true },
          { label: "Source ref", value: deployment.source_git_ref || "unknown", mono: true },
          { label: "Target", value: deployment.deploy.target_name || "unknown" },
          { label: "Target type", value: deployment.deploy.target_type },
          { label: "Deploy mode", value: deployment.deploy.deploy_mode },
          { label: "Deployment id", value: deployment.deploy.deployment_id ?? "unknown", mono: true },
          { label: "Deploy status", value: labelForStatus(deployment.deploy.status), status: deployment.deploy.status },
          { label: "Health status", value: labelForStatus(deployment.destination_health.status), status: deployment.destination_health.status },
          { label: "Health URLs", value: deployment.destination_health.urls.join(", ") || "none", mono: true },
          { label: "Started", value: formatTime(deployment.deploy.started_at ?? "") },
          { label: "Finished", value: formatTime(deployment.deploy.finished_at ?? "") }
        ]
      });
    }
    if (lane?.latest_backup_gate) {
      const backup = lane.latest_backup_gate;
      rows.push({
        id: backup.record_id,
        title: `${lane.instance} backup gate`,
        detail: backup.source,
        status: backup.status,
        time: backup.created_at,
        lane: laneName,
        kind: "backup",
        facts: [
          { label: "Record", value: backup.record_id, mono: true },
          { label: "Source", value: backup.source || "unknown" },
          { label: "Required", value: backup.required ? "yes" : "no" },
          { label: "Status", value: labelForStatus(backup.status), status: backup.status },
          { label: "Created", value: formatTime(backup.created_at) },
          ...Object.entries(backup.evidence).map(([label, value]) => ({
            label,
            value,
            mono: true
          }))
        ]
      });
    }
    if (lane?.latest_promotion) {
      const promotion = lane.latest_promotion;
      rows.push({
        id: promotion.record_id,
        title: `${promotion.from_instance} to ${promotion.to_instance}`,
        detail: promotion.artifact_identity.artifact_id,
        status: promotion.deploy.status,
        time: promotion.deploy.finished_at ?? "",
        lane: "prod",
        kind: "promote",
        facts: [
          { label: "Record", value: promotion.record_id, mono: true },
          { label: "Artifact", value: promotion.artifact_identity.artifact_id, mono: true },
          { label: "From", value: promotion.from_instance },
          { label: "To", value: promotion.to_instance },
          { label: "Deployment record", value: promotion.deployment_record_id ?? "unknown", mono: true },
          { label: "Backup record", value: promotion.backup_record_id ?? "unknown", mono: true },
          { label: "Backup gate", value: labelForStatus(promotion.backup_gate.status), status: promotion.backup_gate.status },
          { label: "Deploy target", value: promotion.deploy.target_name || "unknown" },
          { label: "Deploy status", value: labelForStatus(promotion.deploy.status), status: promotion.deploy.status },
          { label: "Health status", value: labelForStatus(promotion.destination_health.status), status: promotion.destination_health.status },
          { label: "Finished", value: formatTime(promotion.deploy.finished_at ?? "") }
        ]
      });
    }
  });
  previews.forEach((summary) => {
    const generation = summary.latest_generation;
    rows.push({
      id: generation?.generation_id ?? summary.preview.preview_id,
      title: summary.preview.preview_label,
      detail: generation?.artifact_id ?? summary.preview.canonical_url,
      status: generation?.overall_health_status ?? summary.preview.state,
      time: generation?.finished_at ?? summary.preview.updated_at,
      lane: "preview",
      kind: "preview",
      facts: [
        { label: "Preview", value: summary.preview.preview_id, mono: true },
        { label: "Label", value: summary.preview.preview_label },
        { label: "Pull request", value: `${summary.preview.anchor_repo}#${summary.preview.anchor_pr_number}` },
        { label: "URL", value: summary.preview.canonical_url, mono: true },
        { label: "State", value: summary.preview.state },
        { label: "Generation", value: generation?.generation_id ?? "none", mono: true },
        { label: "Sequence", value: generation ? String(generation.sequence) : "none" },
        { label: "Artifact", value: generation?.artifact_id ?? "unknown", mono: true },
        { label: "Deploy", value: labelForStatus(generation?.deploy_status ?? "unknown"), status: generation?.deploy_status ?? "unknown" },
        { label: "Verify", value: labelForStatus(generation?.verify_status ?? "unknown"), status: generation?.verify_status ?? "unknown" },
        { label: "Health", value: labelForStatus(generation?.overall_health_status ?? "unknown"), status: generation?.overall_health_status ?? "unknown" },
        { label: "Updated", value: formatTime(summary.preview.updated_at) },
        { label: "Finished", value: formatTime(generation?.finished_at ?? "") }
      ]
    });
  });
  return rows.sort((left, right) => right.time.localeCompare(left.time)).slice(0, 8);
}

function pickNextAction(actions: DriverActionDescriptor[], verdict: PromotionVerdict): DriverActionDescriptor | undefined {
  if (verdict === "ready") {
    return actions.find((action) => action.action_id === "prod_promotion");
  }
  return (
    actions.find((action) => action.action_id === "prod_backup_gate") ??
    actions.find((action) => action.safety === "safe_write") ??
    actions.find((action) => action.safety === "read")
  );
}

function findDriverView(view: DriverContextView | null, driverId: string): DriverView | null {
  return view?.drivers.find((driver) => driver.driver_id === driverId) ?? null;
}

function artifactFromLane(lane: LaneSummary | null): string {
  return (
    lane?.inventory?.artifact_identity?.artifact_id ??
    lane?.latest_deployment?.artifact_identity?.artifact_id ??
    lane?.latest_promotion?.artifact_identity?.artifact_id ??
    ""
  );
}

function releaseIdentityFromLane(lane: LaneSummary | null): string {
  if (lane?.release_tuple?.tuple_id) {
    return lane.release_tuple.tuple_id;
  }
  const artifact = artifactFromLane(lane);
  if (!artifact) {
    return "";
  }
  return artifact.includes("@") ? artifact.split("@").at(-1) ?? artifact : artifact;
}

function latestEvidenceLabel(prod: LaneSummary | null, testing: LaneSummary | null): string {
  const rows = buildEvidenceRows(prod, testing, []);
  const latest = rows[0];
  if (!latest) {
    return "No promotion evidence has been recorded.";
  }
  return `${latest.lane} ${latest.kind}: ${formatTime(latest.time)}`;
}

function fixtureLane({
  instance,
  artifact,
  deployStatus,
  healthStatus,
  backupStatus
}: {
  instance: "prod" | "testing";
  artifact: string;
  deployStatus: Status;
  healthStatus: Status;
  backupStatus?: Status;
}): LaneSummary {
  const deploy = {
    target_name: `verireel-${instance}`,
    target_type: "application" as const,
    deploy_mode: "runtime-provider-api",
    deployment_id: `fixture-${instance}`,
    status: deployStatus,
    started_at: "2026-04-28T14:30:00Z",
    finished_at: instance === "prod" ? "2026-04-28T14:40:00Z" : "2026-04-28T15:05:00Z"
  };
  const destinationHealth = {
    verified: healthStatus !== "unknown",
    urls: [`https://${instance}.verireel.example/api/health`],
    timeout_seconds: 60,
    status: healthStatus
  };
  return {
    context: "verireel",
    instance,
    inventory: {
      context: "verireel",
      instance,
      artifact_identity: { artifact_id: artifact },
      source_git_ref: "6b3c9d7e8f901234567890abcdef1234567890ab",
      deploy,
      destination_health: destinationHealth,
      updated_at: deploy.finished_at,
      deployment_record_id: `fixture-deployment-${instance}`
    },
    latest_deployment: {
      record_id: `fixture-deployment-${instance}`,
      artifact_identity: { artifact_id: artifact },
      context: "verireel",
      instance,
      source_git_ref: "6b3c9d7e8f901234567890abcdef1234567890ab",
      deploy,
      destination_health: destinationHealth
    },
    latest_backup_gate: backupStatus
      ? {
          record_id: `fixture-backup-${instance}`,
          context: "verireel",
          instance,
          created_at: "2026-04-28T15:07:00Z",
          source: "fixture evidence",
          required: true,
          status: backupStatus,
          evidence: backupStatus === "pass" ? { snapshot: "fixture-prod" } : {}
        }
      : null,
    secret_bindings: []
  };
}

function worstStatus(statuses: Array<Status | string>): Status | string {
  if (statuses.includes("fail")) {
    return "fail";
  }
  if (statuses.includes("pending")) {
    return "pending";
  }
  if (statuses.every((status) => status === "pass")) {
    return "pass";
  }
  return "unknown";
}

function labelForStatus(status: Status | string): string {
  return status.replace("_", " ") || "unknown";
}

function safetyLabel(safety: Safety): string {
  return safety.replace("_", " ").toUpperCase();
}

function shorten(value: string): string {
  if (value.length <= 14) {
    return value;
  }
  return `${value.slice(0, 7)}...${value.slice(-4)}`;
}

function formatTime(value: string): string {
  if (!value) {
    return "unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}
