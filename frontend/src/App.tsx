import {
  Archive,
  ChevronDown,
  Clock3,
  Command,
  Database,
  Eye,
  GitCompareArrows,
  LogOut,
  Loader2,
  Moon,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sun,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  LaunchplaneApiError,
  listEveryCodeWorkRequests,
  listDrivers,
  listProducts,
  listProductProfiles,
  logout,
  readGitHubIssueInbox,
  readAuthSession,
  readDriverView,
  readProductEnvironmentConfigStatus,
} from "./api";
import { ApiErrorPanel, AuthPanel } from "./AuthPanels";
import { formatTime, labelForStatus } from "./format";
import { LanePanel } from "./LanePanel";
import { artifactFromLane, worstStatus } from "./laneSummary";
import { KeyValue, PanelHead } from "./panel-ui";
import { PreviewInventory } from "./PreviewInventory";
import {
  buildPromotionDecision,
  pickNextAction,
  PromotionBridge,
} from "./PromotionBridge";
import { ProductInventoryCockpit } from "./ProductInventoryCockpit";
import { ProductOverviewShell } from "./ProductOverviewShell";
import {
  ProductConfigPanel,
  runtimeScopeForTarget,
  secretScopeForTarget,
} from "./ProductConfigPanel";
import { ProductConfigStatusPanel } from "./ProductConfigStatusPanel";
import { RuntimeAuthorityList } from "./RuntimeAuthorityList";
import { StatusIcon, StatusPill, StateBlock, SkeletonRows } from "./status-ui";
import { TrustBadge } from "./TrustBadge";
import {
  choiceFromProductOverview,
  choiceKey,
  type DriverChoice,
  useProductSelection,
} from "./useProductSelection";
import { useWorkGraphQueue } from "./useWorkGraphQueue";
import type {
  AuthIdentity,
  DataProvenance,
  DriverActionDescriptor,
  DriverContextView,
  DriverDescriptor,
  DriverView,
  EveryCodeWorkRequestRecord,
  GitHubIssueInbox,
  GitHubIssueInboxIssue,
  GitHubIssueInboxReconcileMode,
  GitHubIssueInboxReconcilePayload,
  GitHubIssueInboxReconcileSummary,
  GenericWebProdPromotionPayload,
  GenericWebPromotionWorkflowPayload,
  GenericWebPromotionWorkflowRequest,
  LaneSummary,
  MergeTrainControllerStatusPayload,
  ProductConfigApplyPayload,
  ProductConfigApplyRequest,
  ProductConfigRuntimeInput,
  ProductEnvironmentConfigStatus,
  ProductEnvironmentSummary,
  ProductProfileRecord,
  ProductSiteOverview,
  PreviewSummary,
  Safety,
  Status,
  WorkGraphQueueItem,
} from "./types";
import { type WorkGraphFilter, type WorkGraphMode } from "./WorkGraphQueue";

type Theme = "dark" | "light";
const assetUrl = (path: string) => `${import.meta.env.BASE_URL}${path}`;
const launchplaneIconUrl = assetUrl("assets/brand/launchplane-icon.svg");

type AuthStatus = "checking" | "signed_out" | "signed_in";
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

function requireFixtureProvenance(provenance: DataProvenance | undefined): DataProvenance {
  if (!provenance) {
    throw new Error("Fixture provenance is required.");
  }
  return provenance;
}

const THEME_STORAGE_KEY = "launchplane.theme";
const FIXTURE_ACTIONS: DriverActionDescriptor[] = [
  {
    action_id: "prod_backup_gate",
    label: "Capture prod backup gate",
    description:
      "Capture concrete backup evidence before a prod-changing action.",
    safety: "safe_write",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/verireel/prod-backup-gate",
    writes_records: ["backup_gate"],
  },
  {
    action_id: "prod_promotion",
    label: "Promote testing to prod",
    description:
      "Promote a stored testing artifact to prod after backup-gate evidence passes.",
    safety: "mutation",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/verireel/prod-promotion",
    writes_records: ["deployment", "promotion", "inventory", "release_tuple"],
  },
];
const FIXTURE_GENERIC_WEB_ACTIONS: DriverActionDescriptor[] = [
  {
    action_id: "prod_promotion",
    label: "Promote testing to prod",
    description:
      "Promote a generic-web testing image to prod and record promotion health evidence.",
    safety: "mutation",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/generic-web/prod-promotion",
    writes_records: ["deployment", "promotion", "inventory"],
  },
  {
    action_id: "prod_promotion_workflow",
    label: "Dispatch promote workflow",
    description: "Dispatch the product-owned GitHub promote workflow.",
    safety: "mutation",
    scope: "instance",
    method: "POST",
    route_path: "/v1/drivers/generic-web/prod-promotion-workflow",
    writes_records: [],
  },
];
const FIXTURE_VERIREEL_DRIVER: DriverDescriptor = {
  schema_version: 1,
  driver_id: "verireel",
  base_driver_id: "generic-web",
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
      panels: ["preview_inventory"],
    },
  ],
  actions: FIXTURE_ACTIONS,
  setting_groups: [],
};
const FIXTURE_ODOO_DRIVER: DriverDescriptor = {
  ...FIXTURE_VERIREEL_DRIVER,
  driver_id: "odoo",
  base_driver_id: "",
  label: "Odoo",
  product: "odoo",
  capabilities: [],
  actions: [],
};

export function App() {
  const showFixtureGallery =
    import.meta.env.DEV &&
    new URLSearchParams(window.location.search).get("fixtures") === "1";
  const [theme, setTheme] = useState<Theme>(() => {
    return window.sessionStorage.getItem(THEME_STORAGE_KEY) === "light"
      ? "light"
      : "dark";
  });
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [drivers, setDrivers] = useState<DriverDescriptor[]>([]);
  const [productProfiles, setProductProfiles] = useState<
    ProductProfileRecord[]
  >([]);
  const [productOverviews, setProductOverviews] = useState<
    ProductSiteOverview[]
  >([]);
  const [workRequests, setWorkRequests] = useState<
    EveryCodeWorkRequestRecord[]
  >([]);
  const [configStatuses, setConfigStatuses] = useState<
    ProductEnvironmentConfigStatus[]
  >([]);
  const [configStatusLoading, setConfigStatusLoading] = useState(false);
  const [configStatusError, setConfigStatusError] = useState("");
  const {
    workGraphItems,
    workGraphHiddenCount,
    workGraphError,
    refreshWorkGraph,
    resetWorkGraph,
  } = useWorkGraphQueue();
  const [workGraphFilter, setWorkGraphFilter] =
    useState<WorkGraphFilter>("all");
  const [workGraphMode, setWorkGraphMode] = useState<WorkGraphMode>("all");
  const [issueInbox, setIssueInbox] = useState<GitHubIssueInbox | null>(null);
  const [issueInboxError, setIssueInboxError] = useState("");
  const refreshIssueInbox = useCallback(async (signal?: AbortSignal) => {
    setIssueInboxError("");
    try {
      const payload = await readGitHubIssueInbox(signal);
      if (signal?.aborted) {
        return;
      }
      setIssueInbox(payload.inbox);
    } catch (apiError) {
      if (signal?.aborted) {
        return;
      }
      setIssueInbox(null);
      if (
        apiError instanceof LaunchplaneApiError &&
        apiError.statusCode === 404
      ) {
        setIssueInboxError("");
      } else if (apiError instanceof Error) {
        setIssueInboxError(apiError.message);
      } else {
        setIssueInboxError("GitHub issue inbox request failed.");
      }
    }
  }, []);
  const { choices, selected, setSelected } = useProductSelection({
    drivers,
    productProfiles,
    productOverviews,
  });
  const selectedProductOverview =
    productOverviews.find(
      (overview) => overview.product === selected.driverId,
    ) ?? null;
  const [selectedEnvironment, setSelectedEnvironment] = useState("prod");
  const activeEnvironment =
    selectedProductOverview?.environments.find(
      (environment) => environment.environment === selectedEnvironment,
    ) ?? selectedProductOverview?.environments[0];
  const [prodView, setProdView] = useState<DriverContextView | null>(null);
  const [testingView, setTestingView] = useState<DriverContextView | null>(
    null,
  );
  const [previewView, setPreviewView] = useState<DriverContextView | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");
  const [traceId, setTraceId] = useState<string>("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [reviewAction, setReviewAction] =
    useState<DriverActionDescriptor | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceRow | null>(
    null,
  );
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
        if (
          apiError instanceof LaunchplaneApiError &&
          apiError.statusCode !== 401
        ) {
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
      setProductProfiles([]);
      setProductOverviews([]);
      setConfigStatuses([]);
      setConfigStatusError("");
      setWorkRequests([]);
      resetWorkGraph();
      setIssueInbox(null);
      setIssueInboxError("");
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
      readDriverView(selected.prodContext, "prod"),
      readDriverView(selected.testingContext, "testing"),
      selected.previewContext
        ? readDriverView(selected.previewContext, "").catch(() => null)
        : Promise.resolve(null),
      listProducts().catch(() => ({
        status: "ok" as const,
        trace_id: "",
        products: [],
      })),
      listEveryCodeWorkRequests(8).catch(() => ({
        status: "ok" as const,
        trace_id: "",
        state: "",
        repository: "",
        requests: [],
      })),
      listProductProfiles("generic-web").catch(() => ({
        status: "ok" as const,
        trace_id: "",
        driver_id: "generic-web",
        profiles: [],
      })),
    ])
      .then(
        async ([
          driverPayload,
          prodPayload,
          testingPayload,
          previewPayload,
          productsPayload,
          workRequestsPayload,
          profilePayload,
        ]) => {
          if (controller.signal.aborted) {
            return;
          }
          setDrivers(driverPayload.drivers);
          setProductProfiles(profilePayload.profiles);
          setProdView(prodPayload.view);
          setTestingView(testingPayload.view);
          setPreviewView(previewPayload?.view ?? null);
          setProductOverviews(productsPayload.products);
          setWorkRequests(workRequestsPayload.requests);
          await refreshWorkGraph(controller.signal);
          await refreshIssueInbox(controller.signal);
        },
      )
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
  }, [
    authStatus,
    selected,
    refreshKey,
    refreshWorkGraph,
    refreshIssueInbox,
    resetWorkGraph,
  ]);

  useEffect(() => {
    if (authStatus !== "signed_in" || !selectedProductOverview) {
      setConfigStatuses([]);
      setConfigStatusError("");
      setConfigStatusLoading(false);
      return;
    }
    const controller = new AbortController();
    const environments = selectedProductOverview.environments.filter(
      (environment) => environment.environment.trim(),
    );
    setConfigStatuses([]);
    if (!environments.length) {
      setConfigStatusError("");
      setConfigStatusLoading(false);
      return;
    }
    setConfigStatusLoading(true);
    setConfigStatusError("");
    Promise.allSettled(
      environments.map((environment) =>
        readProductEnvironmentConfigStatus(
          selectedProductOverview.product,
          environment.environment,
          controller.signal,
        ).then((payload) => payload.config_status),
      ),
    )
      .then((results) => {
        if (!controller.signal.aborted) {
          const statuses = results.flatMap((result) =>
            result.status === "fulfilled" ? [result.value] : [],
          );
          const failures = results.flatMap((result, index) =>
            result.status === "rejected"
              ? [
                  configStatusErrorMessage(
                    environments[index].environment,
                    result.reason,
                  ),
                ]
              : [],
          );
          setConfigStatuses(statuses);
          setConfigStatusError(failures[0] ?? "");
        }
      })
      .catch((apiError: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setConfigStatuses([]);
        if (apiError instanceof LaunchplaneApiError) {
          setConfigStatusError(apiError.message);
        } else if (apiError instanceof Error) {
          setConfigStatusError(apiError.message);
        } else {
          setConfigStatusError("Config status request failed.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setConfigStatusLoading(false);
        }
      });
    return () => controller.abort();
  }, [authStatus, selectedProductOverview, refreshKey]);

  useEffect(() => {
    if (
      selectedProductOverview?.environments.length &&
      !selectedProductOverview.environments.some(
        (environment) => environment.environment === selectedEnvironment,
      )
    ) {
      setSelectedEnvironment(
        selectedProductOverview.environments[0].environment,
      );
    }
  }, [selectedProductOverview, selectedEnvironment]);

  const currentDriver = drivers.find(
    (driver) => driver.driver_id === selected.driverId,
  );
  const prodDriverView = findDriverView(prodView, selected.driverId);
  const testingDriverView = findDriverView(testingView, selected.driverId);
  const previewDriverView = findDriverView(previewView, selected.driverId);
  const selectedDriver =
    prodDriverView?.descriptor ??
    testingDriverView?.descriptor ??
    currentDriver;
  const actions = useMemo(() => {
    return (
      selectedDriver?.actions ??
      prodDriverView?.available_actions ??
      testingDriverView?.available_actions ??
      []
    );
  }, [selectedDriver, prodDriverView, testingDriverView]);
  const promotionDecision = useMemo(
    () =>
      buildPromotionDecision(
        prodDriverView?.lane_summary ?? null,
        testingDriverView?.lane_summary ?? null,
        { requireProdBackup: requiresProdBackup(actions) },
      ),
    [actions, prodDriverView, testingDriverView],
  );
  const nextAction = pickNextAction(actions, promotionDecision.verdict);

  function signOut() {
    logout().finally(() => {
      setIdentity(null);
      setAuthStatus("signed_out");
      setDrivers([]);
      setProductProfiles([]);
      setProductOverviews([]);
      setConfigStatuses([]);
      setConfigStatusError("");
      setWorkRequests([]);
      resetWorkGraph();
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
        authenticated={authStatus === "signed_in"}
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
            {error ? (
              <ApiErrorPanel
                message={error}
                traceId={traceId}
                onClearToken={signOut}
              />
            ) : null}
            <ProductInventoryCockpit
              products={productOverviews}
              selectedProduct={selectedProductOverview}
              workRequests={workRequests}
              workGraphItems={workGraphItems}
              workGraphHiddenCount={workGraphHiddenCount}
              workGraphError={workGraphError}
              issueInbox={issueInbox}
              issueInboxError={issueInboxError}
              workGraphFilter={workGraphFilter}
              workGraphMode={workGraphMode}
              loading={loading}
              onSelectProduct={(product) =>
                setSelected(choiceFromProductOverview(product))
              }
              onWorkGraphFilterChange={setWorkGraphFilter}
              onWorkGraphModeChange={setWorkGraphMode}
              onRefreshIssueInbox={() => void refreshIssueInbox()}
            />
            <ProductOverviewShell
              product={selectedProductOverview}
              selected={selected}
              loading={loading}
              selectedEnvironment={
                activeEnvironment?.environment ?? selectedEnvironment
              }
              onSelectEnvironment={setSelectedEnvironment}
            />
            <section className="lane-grid" aria-busy={loading}>
              <LanePanel
                title="Testing"
                laneKind="testing"
                lane={testingDriverView?.lane_summary ?? null}
                loading={loading}
              />
              <PromotionBridge
                prod={prodDriverView?.lane_summary ?? null}
                testing={testingDriverView?.lane_summary ?? null}
                actions={actions}
                environmentActions={activeEnvironment?.available_actions ?? []}
                product={selectedDriver?.product ?? selected.driverId}
                context={selected.prodContext}
                environment={
                  activeEnvironment?.environment ?? selectedEnvironment
                }
                decision={promotionDecision}
                loading={loading}
                onAction={setReviewAction}
              />
              <LanePanel
                title="Prod"
                laneKind="prod"
                lane={prodDriverView?.lane_summary ?? null}
                loading={loading}
              />
            </section>
            <ProductConfigStatusPanel
              statuses={configStatuses}
              selectedEnvironment={
                activeEnvironment?.environment ?? selectedEnvironment
              }
              loading={configStatusLoading}
              error={configStatusError}
            />
            <ProductConfigPanel
              productDefault={selectedDriver?.product ?? selected.driverId}
              contextDefault={selected.prodContext}
              instanceDefault="prod"
              environments={selectedProductOverview?.environments ?? []}
              disabled={loading}
            />
            <section className="work-grid">
              <PreviewInventory
                driver={selectedDriver ?? null}
                previews={previewDriverView?.preview_summaries ?? []}
                inventoryProvenance={
                  previewDriverView?.preview_inventory_provenance ?? null
                }
                loading={loading}
              />
              <ActionList
                actions={actions}
                nextAction={nextAction}
                loading={loading}
                onAction={setReviewAction}
              />
            </section>
            <section className="work-grid work-grid-evidence">
              <RuntimeAuthorityList
                driver={selectedDriver ?? null}
                lane={prodDriverView?.lane_summary ?? null}
              />
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
      <ActionReviewDialog
        action={reviewAction}
        onClose={() => setReviewAction(null)}
      />
      <EvidenceDetailDrawer
        evidence={selectedEvidence}
        onClose={() => setSelectedEvidence(null)}
      />
    </div>
  );
}

function Header({
  choices,
  selected,
  onSelect,
  authenticated,
  theme,
  onThemeChange,
  loading,
  identity,
  onLogout,
  onRefresh,
}: {
  choices: DriverChoice[];
  selected: DriverChoice;
  onSelect: (choice: DriverChoice) => void;
  authenticated: boolean;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  loading: boolean;
  identity: AuthIdentity | null;
  onLogout: () => void;
  onRefresh: () => void;
}) {
  const selectedValue = choiceKey(selected);
  const selectId = "launchplane-product-select";
  const brandTitle = authenticated ? selected.label : "Launchplane";
  const brandMeta = authenticated
    ? [selected.driverLabel, selected.repository ?? "stable lanes"]
    : ["operator access", "authentication required"];

  return (
    <header className="topbar">
      <div className="brand-block">
        <div className="brand-mark" aria-hidden="true">
          <img src={launchplaneIconUrl} alt="" />
        </div>
        <div>
          <div className="brand-title">{brandTitle}</div>
          <div className="brand-meta">
            <span>{brandMeta[0]}</span>
            <span>{brandMeta[1]}</span>
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
        {authenticated ? (
          <>
            <label className="select-label">
              <span>Product</span>
              <select
                id={selectId}
                aria-label="Launchplane product"
                value={selectedValue}
                onChange={(event) => {
                  const choice = choices.find(
                    (item) => choiceKey(item) === event.target.value,
                  );
                  if (choice) {
                    onSelect(choice);
                  }
                }}
              >
                {choices.map((choice) => (
                  <option key={choiceKey(choice)} value={choiceKey(choice)}>
                    {choice.label}
                  </option>
                ))}
              </select>
              <ChevronDown size={14} aria-hidden="true" />
            </label>
            <button
              className="icon-button"
              type="button"
              title="Refresh"
              aria-label={
                loading
                  ? "Refreshing Launchplane view"
                  : "Refresh Launchplane view"
              }
              onClick={onRefresh}
            >
              {loading ? (
                <Loader2 className="spin" size={16} />
              ) : (
                <RefreshCw size={16} />
              )}
            </button>
          </>
        ) : null}
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
          aria-label={
            theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
          }
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

function ActionList({
  actions,
  nextAction,
  loading,
  onAction,
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
              <code>
                {nextAction.method} {nextAction.route_path}
              </code>
            </button>
          ) : null}
          {groups.map((safety) => {
            const groupedActions = actions.filter(
              (action) => action.safety === safety,
            );
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

function EvidenceTimeline({
  prod,
  testing,
  previews,
  onSelect,
}: {
  prod: LaneSummary | null;
  testing: LaneSummary | null;
  previews: PreviewSummary[];
  onSelect: (row: EvidenceRow) => void;
}) {
  const rows = buildEvidenceRows(prod, testing, previews);
  return (
    <section className="panel">
      <PanelHead
        eyebrow="evidence"
        title="Timeline"
        right={<Database size={17} aria-hidden="true" />}
      />
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
                  <span className={`lane-chip lane-chip-${row.lane}`}>
                    {row.lane}
                  </span>
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

function EvidenceDetailDrawer({
  evidence,
  onClose,
}: {
  evidence: EvidenceRow | null;
  onClose: () => void;
}) {
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
          <button
            className="icon-button"
            type="button"
            aria-label="Close evidence detail"
            onClick={onClose}
          >
            <XCircle size={15} aria-hidden="true" />
          </button>
        </div>
        <div className="drawer-meta">
          <span className={`lane-chip lane-chip-${evidence.lane}`}>
            {evidence.lane}
          </span>
          <StatusPill status={evidence.status} />
          <code>{evidence.kind}</code>
          <code>{formatTime(evidence.time)}</code>
        </div>
        <p className="drawer-summary">
          {evidence.detail || "No detail recorded for this evidence row."}
        </p>
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

function configStatusErrorMessage(
  environment: string,
  apiError: unknown,
): string {
  const prefix = `${environment} config status`;
  if (apiError instanceof LaunchplaneApiError) {
    return `${prefix}: ${apiError.message}`;
  }
  if (apiError instanceof Error) {
    return `${prefix}: ${apiError.message}`;
  }
  return `${prefix}: request failed.`;
}

function StateFixtureGallery({
  actions,
}: {
  actions: DriverActionDescriptor[];
}) {
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceRow | null>(
    null,
  );
  const [fixtureWorkGraphFilter, setFixtureWorkGraphFilter] =
    useState<WorkGraphFilter>("all");
  const [fixtureWorkGraphMode, setFixtureWorkGraphMode] =
    useState<WorkGraphMode>("all");
  const [fixtureEnvironment, setFixtureEnvironment] = useState("prod");
  const readyProd = fixtureLane({
    instance: "prod",
    artifact: "ghcr.io/every/verireel@sha256:11112222",
    deployStatus: "pass",
    healthStatus: "pass",
    backupStatus: "pass",
  });
  const readyTesting = fixtureLane({
    instance: "testing",
    artifact: "ghcr.io/every/verireel@sha256:aa55cc77",
    deployStatus: "pass",
    healthStatus: "pass",
  });
  const missingBackupProd = fixtureLane({
    instance: "prod",
    artifact: "ghcr.io/every/verireel@sha256:11112222",
    deployStatus: "pass",
    healthStatus: "pass",
  });
  const failedTesting = fixtureLane({
    instance: "testing",
    artifact: "ghcr.io/every/verireel@sha256:fail0001",
    deployStatus: "fail",
    healthStatus: "fail",
  });
  const configEnvironments: ProductEnvironmentSummary[] = [
    {
      environment: "testing",
      context: "sellyouroutboard-testing",
      base_url: "https://testing.sellyouroutboard.example",
      health_url: "https://testing.sellyouroutboard.example/healthz",
      trust_state: "verified",
      provenance: requireFixtureProvenance(readyTesting.provenance),
      warnings: [],
      available_actions: FIXTURE_GENERIC_WEB_ACTIONS.map((action) => ({
        ...action,
        authz_action: "product_environment.write",
        enabled: action.action_id === "prod_promotion_workflow",
        disabled_reasons:
          action.action_id === "prod_promotion"
            ? ["Prod promotion runs through the product-owned workflow."]
            : [],
        trust_state: "verified",
      })),
    },
    {
      environment: "prod",
      context: "sellyouroutboard-prod",
      base_url: "https://sellyouroutboard.example",
      health_url: "https://sellyouroutboard.example/healthz",
      trust_state: "verified",
      provenance: requireFixtureProvenance(readyProd.provenance),
      warnings: [],
      available_actions: FIXTURE_GENERIC_WEB_ACTIONS.map((action) => ({
        ...action,
        authz_action: "product_environment.write",
        enabled: false,
        disabled_reasons: ["Prod lane writes require fresh backup evidence."],
        trust_state: "recorded",
      })),
    },
  ];
  const fixtureProducts: ProductSiteOverview[] = [
    {
      schema_version: 1,
      product: "sellyouroutboard",
      display_name: "SellYourOutboard",
      repository: "cbusillo/sellyouroutboard",
      driver_id: "generic-web",
      base_driver_id: "generic-web",
      environments: configEnvironments,
      preview: {
        enabled: true,
        context: "sellyouroutboard-preview",
        slug_template: "pr-{number}",
        active_count: 3,
        latest_preview_id: "preview-pr-190",
        trust_state: "recorded",
        provenance: requireFixtureProvenance(readyTesting.provenance),
      },
      warnings: [],
      trust_state: "verified",
      provenance: requireFixtureProvenance(readyProd.provenance),
      available_actions: [],
    },
    {
      schema_version: 1,
      product: "verireel",
      display_name: "VeriReel",
      repository: "cbusillo/verireel",
      driver_id: "verireel",
      base_driver_id: "generic-web",
      environments: configEnvironments.map((environment) => ({
        ...environment,
        context:
          environment.environment === "prod" ? "verireel" : "verireel-testing",
        trust_state:
          environment.environment === "prod" ? "recorded" : "verified",
      })),
      preview: {
        enabled: true,
        context: "verireel-testing",
        slug_template: "pr-{number}",
        active_count: 1,
        latest_preview_id: "preview-verireel-168",
        trust_state: "recorded",
        provenance: requireFixtureProvenance(readyTesting.provenance),
      },
      warnings: [
        "Runtime owner route evidence is recorded, not provider verified.",
      ],
      trust_state: "recorded",
      provenance: requireFixtureProvenance(readyTesting.provenance),
      available_actions: [],
    },
  ];
  const fixtureConfigStatuses: ProductEnvironmentConfigStatus[] = [
    {
      schema_version: 1,
      product: "sellyouroutboard",
      display_name: "SellYourOutboard",
      repository: "cbusillo/sellyouroutboard",
      driver_id: "generic-web",
      base_driver_id: "generic-web",
      environment: "testing",
      context: "sellyouroutboard-testing",
      runtime_settings: [
        {
          key: "RESEND_FROM_EMAIL",
          status: "configured",
          context: "sellyouroutboard-testing",
          instance: "testing",
          source_label: "product-config-api",
          updated_at: "2026-05-07T22:32:00Z",
          trust_state: "recorded",
        },
      ],
      managed_secrets: [
        {
          binding_key: "RESEND_API_KEY",
          status: "configured",
          integration: "runtime_environment",
          context: "sellyouroutboard-testing",
          instance: "testing",
          updated_at: "2026-05-07T22:31:00Z",
          trust_state: "recorded",
        },
      ],
      warnings: [],
      trust_state: "recorded",
      provenance: requireFixtureProvenance(readyTesting.provenance),
    },
    {
      schema_version: 1,
      product: "sellyouroutboard",
      display_name: "SellYourOutboard",
      repository: "cbusillo/sellyouroutboard",
      driver_id: "generic-web",
      base_driver_id: "generic-web",
      environment: "prod",
      context: "sellyouroutboard-prod",
      runtime_settings: [
        {
          key: "RESEND_FROM_EMAIL",
          status: "configured",
          context: "sellyouroutboard-prod",
          instance: "prod",
          source_label: "product-config-api",
          updated_at: "2026-05-07T22:36:00Z",
          trust_state: "recorded",
        },
      ],
      managed_secrets: [
        {
          binding_key: "RESEND_API_KEY",
          status: "missing",
          integration: "runtime_environment",
          context: "sellyouroutboard-prod",
          instance: "prod",
          updated_at: "",
          trust_state: "missing",
        },
      ],
      warnings: [],
      trust_state: "missing",
      provenance: requireFixtureProvenance(missingBackupProd.provenance),
    },
  ];
  const fixtureRequests: EveryCodeWorkRequestRecord[] = [
    {
      schema_version: 1,
      request_id: "every-code-cbusillo-launchplane-153-fixture",
      source: "github_issue_label",
      state: "running",
      repository: "cbusillo/launchplane",
      issue_number: 153,
      issue_url: "https://github.com/cbusillo/launchplane/issues/153",
      issue_title: "Rebuild operator UI around product environments",
      trigger_label: "every-code",
      trigger_actor: "code",
      github_delivery_id: "fixture-delivery",
      queued_at: "2026-05-06T00:00:00Z",
      updated_at: "2026-05-06T00:12:00Z",
      claimed_at: "2026-05-06T00:01:00Z",
      claimed_by_host: "local-mac",
      started_at: "2026-05-06T00:02:00Z",
      finished_at: "",
      result_pr_url: "",
      result_summary: "",
      error_message: "",
    },
  ];
  const fixtureWorkGraphItems: WorkGraphQueueItem[] = [
    {
      repository: "cbusillo/launchplane",
      repo_classification: "managed_runtime",
      product: "launchplane",
      product_display_name: "Launchplane",
      number: 190,
      title: "Build What To Work On Next cockpit",
      url: "https://github.com/cbusillo/launchplane/issues/190",
      focus: "Now",
      manager: "Code",
      finish_line: "Ranked queue visible in the operator UI.",
      state: "ready",
      recommendation: "deep_work",
      score: 126,
      updated_at: "2026-05-06T02:12:00Z",
      safe_to_start: true,
      next_action:
        "Continue the active implementation path from the linked source of truth.",
      why_now: "It is marked as the active focus item.",
      blocked_by_count: 0,
      source_of_truth_url: "https://github.com/cbusillo/launchplane/issues/190",
      handoff_url: "https://github.com/cbusillo/launchplane/issues/190",
      evidence: [
        {
          code: "source_of_truth",
          state: "verified",
          detail: "Linked GitHub issue is the source of truth.",
          source_url: "https://github.com/cbusillo/launchplane/issues/190",
        },
      ],
      reasons: [{ code: "focus", detail: "Now lane" }],
    },
    {
      repository: "cbusillo/verireel",
      repo_classification: "managed_runtime",
      product: "verireel",
      product_display_name: "VeriReel",
      number: 169,
      title: "Stabilize app-maintenance E2E cleanup",
      url: "https://github.com/cbusillo/verireel/pull/169",
      focus: "Next",
      manager: "Code",
      finish_line: "Testing image publish is green.",
      state: "ready",
      recommendation: "quick_win",
      score: 110,
      updated_at: "2026-05-06T01:03:00Z",
      safe_to_start: true,
      next_action: "Start a focused branch from the linked source of truth.",
      why_now: "It is ready, focused, and has no tracked subissues.",
      blocked_by_count: 0,
      source_of_truth_url: "https://github.com/cbusillo/verireel/pull/169",
      handoff_url: "https://github.com/cbusillo/verireel/pull/169",
      evidence: [
        {
          code: "source_of_truth",
          state: "verified",
          detail: "Linked GitHub pull request is the source of truth.",
          source_url: "https://github.com/cbusillo/verireel/pull/169",
        },
      ],
      reasons: [{ code: "small", detail: "No subissues" }],
    },
    {
      repository: "cbusillo/launchplane",
      repo_classification: "managed_runtime",
      product: "launchplane",
      product_display_name: "Launchplane",
      number: 248,
      title: "Finish runtime key safety rollout",
      url: "https://github.com/cbusillo/launchplane/issues/248",
      focus: "Waiting",
      manager: "Code",
      finish_line: "All runtime secret paths are gated.",
      state: "blocked",
      recommendation: "blocked_cleanup",
      score: 42,
      updated_at: "2026-05-05T23:00:00Z",
      safe_to_start: false,
      next_action:
        "Inspect the blocking dependency before starting implementation.",
      why_now:
        "The item is visible because dependency cleanup may unblock later work.",
      blocked_by_count: 1,
      source_of_truth_url: "https://github.com/cbusillo/launchplane/issues/248",
      handoff_url: "https://github.com/cbusillo/launchplane/issues/248",
      evidence: [
        {
          code: "blocked_by",
          state: "verified",
          detail: "Blocked by one dependency item.",
          source_url: "https://github.com/cbusillo/launchplane/issues/248",
        },
      ],
      reasons: [
        { code: "blocked", detail: "Waiting on downstream validation" },
      ],
    },
    {
      repository: "cbusillo/odoo-tenant-opw",
      repo_classification: "active_awareness",
      product: "",
      product_display_name: "",
      number: 302,
      title: "Revisit tenant thinning follow-up",
      url: "https://github.com/cbusillo/odoo-tenant-opw/issues/302",
      focus: "Later",
      manager: "Unassigned",
      finish_line:
        "Choose whether this belongs in Launchplane or stays tenant-owned.",
      state: "ready",
      recommendation: "switch_projects",
      score: 58,
      updated_at: "2026-05-05T21:30:00Z",
      safe_to_start: false,
      next_action: "Review the linked source of truth before changing code.",
      why_now: "It is ranked from compact planning and operational signals.",
      blocked_by_count: 0,
      source_of_truth_url:
        "https://github.com/cbusillo/odoo-tenant-opw/issues/302",
      handoff_url: "https://github.com/cbusillo/odoo-tenant-opw/issues/302",
      evidence: [
        {
          code: "source_of_truth",
          state: "verified",
          detail: "Linked GitHub issue is the source of truth.",
          source_url: "https://github.com/cbusillo/odoo-tenant-opw/issues/302",
        },
      ],
      reasons: [
        { code: "repo_classification", detail: "Awareness-only repo." },
      ],
    },
  ];
  const fixtureIssueInbox: GitHubIssueInbox = {
    schema_version: 1,
    generated_at: "2026-05-21T12:30:00Z",
    project_configured: true,
    repository_count: 3,
    issue_count: 5,
    stale_project_item_count: 2,
    repositories: [
      {
        repository: "cbusillo/launchplane",
        issue_count: 3,
        present_in_project_count: 2,
        missing_from_project_count: 1,
        issues: [
          fixtureInboxIssue({
            number: 697,
            title: "Add read-only grouped GitHub issue inbox",
            projectStatus: "present",
          }),
          fixtureInboxIssue({
            number: 698,
            title: "Reconcile missing GitHub issues into Code Plans",
            projectStatus: "missing",
          }),
          fixtureInboxIssue({
            number: 601,
            title: "Retired Code Plans follow-up",
            projectStatus: "closed",
            state: "closed",
          }),
        ],
      },
      {
        repository: "cbusillo/codex-skills",
        issue_count: 1,
        present_in_project_count: 1,
        missing_from_project_count: 0,
        issues: [
          fixtureInboxIssue({
            repository: "cbusillo/codex-skills",
            number: 127,
            title: "Update Launchplane merge train guidance",
            projectStatus: "present",
          }),
        ],
      },
      {
        repository: "cbusillo/side-repo-with-a-long-name-for-mobile-layout",
        issue_count: 1,
        present_in_project_count: 1,
        missing_from_project_count: 0,
        issues: [
          fixtureInboxIssue({
            repository: "cbusillo/side-repo-with-a-long-name-for-mobile-layout",
            number: 12,
            title:
              "Project item no longer appears in the configured open issue inventory",
            projectStatus: "stale",
          }),
        ],
      },
    ],
  };

  return (
    <section className="fixture-gallery">
      <PanelHead
        eyebrow="development fixtures"
        title="Operator state coverage"
      />
      <div className="fixture-wide">
        <ProductInventoryCockpit
          products={fixtureProducts}
          selectedProduct={fixtureProducts[0]}
          workRequests={fixtureRequests}
          workGraphItems={fixtureWorkGraphItems}
          workGraphHiddenCount={2}
          workGraphError=""
          issueInbox={fixtureIssueInbox}
          issueInboxError=""
          workGraphFilter={fixtureWorkGraphFilter}
          workGraphMode={fixtureWorkGraphMode}
          loading={false}
          onSelectProduct={() => undefined}
          onWorkGraphFilterChange={setFixtureWorkGraphFilter}
          onWorkGraphModeChange={setFixtureWorkGraphMode}
          onRefreshIssueInbox={() => undefined}
          reconcileIssueInbox={fixtureReconcileIssueInbox}
          readMergeTrainStatus={fixtureMergeTrainControllerStatus}
          readMergeTrainPolicyTargets={fixtureMergeTrainPolicyTargets}
        />
      </div>
      <div className="fixture-grid">
        <div className="fixture-card">
          <PromotionBridge
            prod={missingBackupProd}
            testing={readyTesting}
            actions={actions}
            product="verireel"
            context="verireel"
            decision={buildPromotionDecision(missingBackupProd, readyTesting)}
            loading={false}
            onAction={() => undefined}
          />
        </div>
        <div className="fixture-card">
          <PromotionBridge
            prod={readyProd}
            testing={failedTesting}
            actions={actions}
            product="verireel"
            context="verireel"
            decision={buildPromotionDecision(readyProd, failedTesting)}
            loading={false}
            onAction={() => undefined}
          />
        </div>
        <div className="fixture-card">
          <PromotionBridge
            prod={missingBackupProd}
            testing={readyTesting}
            actions={FIXTURE_GENERIC_WEB_ACTIONS}
            environmentActions={configEnvironments[1].available_actions}
            product="sellyouroutboard"
            context="sellyouroutboard-testing"
            environment="prod"
            decision={buildPromotionDecision(missingBackupProd, readyTesting, {
              requireProdBackup: false,
            })}
            loading={false}
            onAction={() => undefined}
            dryRunPromotion={fixtureGenericWebPromotionDryRun}
            dispatchWorkflow={fixtureGenericWebPromotionWorkflow}
          />
        </div>
        <div className="fixture-card">
          <LanePanel
            title="Failed testing lane"
            laneKind="testing"
            lane={failedTesting}
            loading={false}
          />
        </div>
        <div className="fixture-card">
          <PreviewInventory
            driver={FIXTURE_VERIREEL_DRIVER}
            previews={[]}
            inventoryProvenance={null}
            loading={false}
          />
        </div>
        <div className="fixture-card">
          <PreviewInventory
            driver={FIXTURE_ODOO_DRIVER}
            previews={[]}
            inventoryProvenance={null}
            loading={false}
          />
        </div>
      </div>
      <div className="fixture-wide">
        <RuntimeAuthorityList
          driver={FIXTURE_VERIREEL_DRIVER}
          lane={readyProd}
        />
      </div>
      <div className="fixture-wide">
        <ProductOverviewShell
          product={fixtureProducts[0]}
          selected={choiceFromProductOverview(fixtureProducts[0])}
          loading={false}
          selectedEnvironment={fixtureEnvironment}
          onSelectEnvironment={setFixtureEnvironment}
        />
      </div>
      <div className="fixture-wide">
        <EvidenceTimeline
          prod={readyProd}
          testing={readyTesting}
          previews={[]}
          onSelect={setSelectedEvidence}
        />
      </div>
      <div className="fixture-wide">
        <ProductConfigStatusPanel
          statuses={fixtureConfigStatuses}
          selectedEnvironment={fixtureEnvironment}
          loading={false}
          error=""
        />
      </div>
      <div className="fixture-wide">
        <ProductConfigPanel
          productDefault="sellyouroutboard"
          contextDefault="sellyouroutboard-testing"
          instanceDefault="prod"
          environments={configEnvironments}
          disabled={false}
          applyConfig={fixtureProductConfigApply}
        />
      </div>
      <EvidenceDetailDrawer
        evidence={selectedEvidence}
        onClose={() => setSelectedEvidence(null)}
      />
    </section>
  );
}

function fixtureMergeTrainControllerStatus(
  repository = "cbusillo/launchplane",
  baseBranch = "main",
): Promise<MergeTrainControllerStatusPayload> {
  const policyKey = `${repository}:${baseBranch}`;
  return Promise.resolve({
    status: "ok",
    trace_id: "fixture-trace-merge-train-status",
    controller_status: {
      schema_version: 1,
      repository,
      base_branch: baseBranch,
      generated_at: "2026-05-20T16:20:00Z",
      current_policy_key: policyKey,
      current_policy_sha256: "fixture-policy-sha256",
      admission: {
        schema_version: 1,
        repository,
        base_branch: baseBranch,
        status: "admitted",
        reason_code: "poll_interval_elapsed",
        requested_at: "2026-05-20T16:20:00Z",
        next_allowed_at: "2026-05-20T16:20:00Z",
        latest_run_id: "merge-train-run-fixture",
        latest_run_status: "waiting",
        latest_run_recorded_at: "2026-05-20T16:14:00Z",
        controller_action: "observe_candidate",
        controller_reason: "candidate checks are still pending",
        controller_candidate_record_id: "merge-train-batch-candidate-fixture",
        controller_landing_plan_record_id: "",
        controller_stack_collapse_plan_record_id: "",
        detail:
          "Merge-train poll interval has elapsed for the latest wait boundary.",
      },
      latest_run: {
        schema_version: 1,
        run_id: "merge-train-run-fixture",
        trace_id: "fixture-trace-merge-train-run",
        repository,
        base_branch: baseBranch,
        mode: "mutate",
        status: "waiting",
        recorded_at: "2026-05-20T16:14:00Z",
        intended_next_action: "wait_for_checks",
        selected_pr_number: 762,
        selected_head_sha: "abc123fixture",
        selected_mergeable: "mergeable",
        selected_required_checks_status: "pending",
        reread_required: false,
        poll_required: true,
        policy_key: policyKey,
        policy_sha256: "fixture-policy-sha256",
        dry_run_result: {},
        worker_step_result: null,
        snapshot: {},
      },
      latest_dry_run: {
        intended_next_action: "wait_for_checks",
        next_action_detail: "Candidate checks are still pending.",
        queue_count: 3,
        eligible_count: 3,
        selected_pr_number: 762,
        queue_entries: [
          {
            pull_request_number: 762,
            title: "Post merge train controller feedback",
            url: "https://github.com/cbusillo/launchplane/pull/762",
            eligible: true,
            ineligible_reasons: [],
            mergeable: "mergeable",
            required_checks_status: "pending",
          },
          {
            pull_request_number: 763,
            title: "Post feedback for manual merge train phases",
            url: "https://github.com/cbusillo/launchplane/pull/763",
            eligible: true,
            ineligible_reasons: [],
            mergeable: "mergeable",
            required_checks_status: "pass",
          },
        ],
      },
      controller_state: null,
      controller_diagnostics: null,
      controller_records: [
        {
          record_id: "merge-train-batch-candidate-fixture",
          record_type: "batch_candidate",
          status: "active",
          updated_at: "2026-05-20T16:14:00Z",
          policy_key: policyKey,
          policy_sha256: "fixture-policy-sha256",
          policy_status: "current",
          stale_reason: "",
          batch_id: "merge-train-batch-fixture",
          pull_request_numbers: [762, 763, 764],
          candidate_sha: "abc123fixture",
          required_checks_status: "pending",
          planned_count: 3,
          merged_count: 1,
          blocked_count: 0,
          stale_count: 0,
          skipped_count: 0,
        },
      ],
    },
  });
}

function fixtureInboxIssue({
  repository = "cbusillo/launchplane",
  number,
  title,
  projectStatus,
  state = "open",
}: {
  repository?: string;
  number: number;
  title: string;
  projectStatus: GitHubIssueInboxIssue["project_status"];
  state?: string;
}): GitHubIssueInboxIssue {
  return {
    key: `${repository}#${number}`,
    repository,
    number,
    title,
    url: `https://github.com/${repository}/issues/${number}`,
    state,
    labels: ["plan"],
    author: projectStatus === "stale" ? "automation-gh" : "code",
    created_at: "2026-05-20T12:00:00Z",
    updated_at: "2026-05-21T12:00:00Z",
    project_status: projectStatus,
    present_in_project:
      projectStatus !== "missing" && projectStatus !== "unconfigured",
  };
}

function fixtureReconcileIssueInbox(
  mode: GitHubIssueInboxReconcileMode,
): Promise<GitHubIssueInboxReconcilePayload> {
  const summary: GitHubIssueInboxReconcileSummary = {
    schema_version: 1,
    generated_at: "2026-05-21T12:32:00Z",
    mode,
    repository_count: 3,
    issue_count: 5,
    added_count: mode === "apply" ? 1 : 0,
    already_present_count: mode === "apply" ? 2 : 0,
    skipped_count: 0,
    failed_count: mode === "apply" ? 1 : 0,
    would_add_count: mode === "dry_run" ? 1 : 0,
    items: [
      {
        key: "cbusillo/launchplane#698",
        repository: "cbusillo/launchplane",
        number: 698,
        title: "Reconcile missing GitHub issues into Code Plans",
        url: "https://github.com/cbusillo/launchplane/issues/698",
        action: mode === "dry_run" ? "would_add" : "added",
        detail: "",
        error_code: "",
        error_correlation_id: "",
      },
      {
        key: "cbusillo/private-repo#44",
        repository: "cbusillo/private-repo",
        number: 44,
        title: "Project write denied for fixture",
        url: "https://github.com/cbusillo/private-repo/issues/44",
        action: mode === "dry_run" ? "would_add" : "failed",
        detail: mode === "apply" ? "GitHub CLI returned 403." : "",
        error_code: mode === "apply" ? "authorization_denied" : "",
        error_correlation_id: "",
      },
    ],
  };
  return Promise.resolve({
    status: "accepted",
    trace_id: "fixture-trace-issue-inbox-reconcile",
    records: {},
    result: { reconcile: summary },
  });
}

function fixtureMergeTrainPolicyTargets() {
  return Promise.resolve({
    status: "ok",
    trace_id: "fixture-trace-merge-train-policy-targets",
    policy: {
      record_id: "merge-train-policy-fixture",
      updated_at: "2026-05-20T16:18:00Z",
      policy_sha256: "fixture-policy-sha256",
    },
    targets: [
      {
        repository: "cbusillo/launchplane",
        base_branch: "main",
        policy_key: "cbusillo/launchplane:main",
        service_authz: {
          action: "merge_train.run_once",
          product: "launchplane",
          context: "launchplane",
        },
      },
      {
        repository: "cbusillo/sellyouroutboard",
        base_branch: "release",
        policy_key: "cbusillo/sellyouroutboard:release",
        service_authz: {
          action: "merge_train.run_once",
          product: "launchplane",
          context: "launchplane",
        },
      },
    ],
  });
}

function fixtureGenericWebPromotionDryRun(): Promise<GenericWebProdPromotionPayload> {
  return Promise.resolve({
    status: "accepted",
    trace_id: "fixture-trace-generic-web-promote",
    records: {
      promotion_record_id: "",
      deployment_record_id: "",
      backup_record_id: "",
      inventory_record_id: "",
      promotion_status: "pending",
      deployment_status: "skipped",
      backup_status: "skipped",
      source_health_status: "pass",
      destination_health_status: "skipped",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      dry_run: "True",
    },
    result: {
      product: "fixture-product",
      context: "fixture-testing",
      from_instance: "testing",
      to_instance: "prod",
      artifact_id: "fixture-artifact",
      source_git_ref: "fixture-sha",
      backup_record_id: "",
      promotion_record_id: "",
      deployment_record_id: "",
      inventory_record_id: "",
      promotion_status: "pending",
      deployment_status: "skipped",
      source_health_status: "pass",
      destination_health_status: "skipped",
      backup_status: "skipped",
      release_status: "skipped",
      release_tag: "",
      release_url: "",
      target_name: "",
      target_id: "",
      target_category: "unknown",
      provider_id: "",
      provider_target_type: "",
      dry_run: true,
      error_message: "",
    },
  });
}

function fixtureGenericWebPromotionWorkflow(
  payload: GenericWebPromotionWorkflowRequest,
): Promise<GenericWebPromotionWorkflowPayload> {
  return Promise.resolve({
    status: "accepted",
    trace_id: "fixture-trace-generic-web-workflow",
    records: {},
    result: {
      product: payload.product,
      context: payload.workflow.context,
      repository: "cbusillo/sellyouroutboard",
      workflow_id: "promote-prod.yml",
      ref: "main",
      dry_run: payload.workflow.dry_run ?? true,
      bump: payload.workflow.bump ?? "patch",
      dispatch_status: "dispatched",
      run_id: 25237186636,
      run_url:
        "https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
      run_status: "queued",
      run_conclusion: "",
    },
  });
}

function fixtureProductConfigApply(
  payload: ProductConfigApplyRequest,
): Promise<ProductConfigApplyPayload> {
  const context = payload.context ?? "";
  const instance = payload.instance ?? "";
  const runtimeInput = payload.runtime_env;
  const structuredRuntimeInput =
    runtimeInput &&
    "env" in runtimeInput &&
    typeof runtimeInput.env === "object" &&
    runtimeInput.env !== null
      ? (runtimeInput as ProductConfigRuntimeInput)
      : null;
  const runtimeEnv = structuredRuntimeInput?.env ??
    (runtimeInput
      ? Object.fromEntries(
          Object.entries(runtimeInput).filter(
            ([key]) => !["scope", "context", "instance"].includes(key),
          ),
        )
      : {});
  const runtimeKeys = Object.keys(runtimeEnv);
  const response = {
    status: "ok",
    mode: payload.mode,
    product: payload.product,
    context,
    instance,
    actor: "fixture-operator",
    source_label: payload.source_label ?? "product-config-ui",
    runtime_environment: {
      action: "updated",
      scope:
        structuredRuntimeInput?.scope ??
        runtimeScopeForTarget(context, instance),
      context,
      instance,
      keys: runtimeKeys,
      changed_keys: runtimeKeys,
      unchanged_keys: [],
      env_value_count_after: runtimeKeys.length,
      record: null,
    },
    runtime_key_safety: {
      required: false,
      status: "skipped",
      policy_record_id: "",
      policy_sha256: "",
      target: null,
      checked_binding_keys: [],
      findings: [],
    },
    secrets: (payload.secrets ?? []).map((secret, index) => {
      const bindingKey = secret.binding_key ?? secret.name ?? "";
      return {
        action: "rotated",
        scope:
          secret.scope ?? secretScopeForTarget(context, instance),
        context,
        instance,
        integration: secret.integration ?? "runtime_environment",
        name: secret.name ?? bindingKey,
        binding_key: bindingKey,
        secret_id: `fixture-secret-${index + 1}`,
      };
    }),
    summary: {
      runtime_changed_key_count: runtimeKeys.length,
      secret_change_count: (payload.secrets ?? []).length,
    },
    next_actions: [],
  } satisfies ProductConfigApplyPayload;
  return Promise.resolve(response);
}

function ActionReviewDialog({
  action,
  onClose,
}: {
  action: DriverActionDescriptor | null;
  onClose: () => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [operatorReason, setOperatorReason] = useState("");
  const requiresTypedConfirmation =
    action?.safety === "destructive" || action?.safety === "mutation";
  const requiresReason = action?.safety === "destructive";
  const confirmationPhrase = action ? `confirm ${action.action_id}` : "";
  const canConfirm =
    !requiresTypedConfirmation ||
    (confirmation === confirmationPhrase &&
      (!requiresReason || operatorReason.trim().length > 0));

  useEffect(() => {
    setConfirmation("");
    setOperatorReason("");
  }, [action]);

  if (!action) {
    return null;
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="action-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <PanelHead
          eyebrow={safetyLabel(action.safety)}
          title={action.label}
          right={<SafetyIcon safety={action.safety} />}
        />
        <p>{action.description}</p>
        {requiresTypedConfirmation ? (
          <div className="risk-callout" data-safety={action.safety}>
            <SafetyIcon safety={action.safety} />
            <div>
              <strong>
                {action.safety === "destructive"
                  ? "Destructive operator review"
                  : "Mutation review"}
              </strong>
              <span>
                This prepares a non-executing request preview for an action that
                can change Launchplane records.
              </span>
            </div>
          </div>
        ) : null}
        <div className="dialog-facts">
          <KeyValue label="Action" value={action.action_id} mono />
          <KeyValue
            label="Safety"
            value={safetyLabel(action.safety)}
            status={action.safety === "destructive" ? "blocked" : "unknown"}
          />
          <KeyValue
            label="Route"
            value={`${action.method} ${action.route_path}`}
            mono
          />
          <KeyValue label="Scope" value={action.scope} />
          <KeyValue
            label="Writes"
            value={action.writes_records?.join(", ") || "none"}
            mono
          />
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
              <input
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
          </div>
        ) : null}
        <div className="dialog-actions">
          <button
            className="button"
            type="button"
            aria-label="Cancel action review"
            onClick={onClose}
          >
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

function buildEvidenceRows(
  prod: LaneSummary | null,
  testing: LaneSummary | null,
  previews: PreviewSummary[],
): EvidenceRow[] {
  const rows: EvidenceRow[] = [];
  [
    { lane: prod, laneName: "prod" },
    { lane: testing, laneName: "testing" },
  ].forEach(({ lane, laneName }) => {
    if (lane?.inventory) {
      const inventory = lane.inventory;
      const inventoryHealth = inventory.destination_health ?? {
        status: "unknown",
        urls: [],
      };
      const inventoryDeployStatus = inventory.deploy.status ?? "unknown";
      const inventoryHealthStatus = inventoryHealth.status ?? "unknown";
      rows.push({
        id: `${inventory.context}:${inventory.instance}:inventory`,
        title: `${lane.instance} inventory`,
        detail:
          inventory.deploy.target_name ||
          artifactFromLane(lane) ||
          "recorded inventory",
        status: worstStatus([
          inventoryDeployStatus,
          inventoryHealthStatus,
        ]),
        time: inventory.updated_at,
        lane: laneName,
        kind: "inventory",
        facts: [
          { label: "Context", value: inventory.context },
          { label: "Instance", value: inventory.instance },
          {
            label: "Artifact",
            value: inventory.artifact_identity?.artifact_id ?? "unknown",
            mono: true,
          },
          { label: "Source ref", value: inventory.source_git_ref, mono: true },
          {
            label: "Deployment record",
            value: inventory.deployment_record_id,
            mono: true,
          },
          {
            label: "Promotion record",
            value: inventory.promotion_record_id ?? "none",
            mono: true,
          },
          {
            label: "Promoted from",
            value: inventory.promoted_from_instance ?? "none",
          },
          {
            label: "Deploy status",
            value: labelForStatus(inventoryDeployStatus),
            status: inventoryDeployStatus,
          },
          {
            label: "Health status",
            value: labelForStatus(inventoryHealthStatus),
            status: inventoryHealthStatus,
          },
          {
            label: "Health URLs",
            value: inventoryHealth.urls?.join(", ") || "none",
            mono: true,
          },
          { label: "Updated", value: formatTime(inventory.updated_at) },
        ],
      });
    }
    if (lane?.release_tuple) {
      const release = lane.release_tuple;
      rows.push({
        id: release.tuple_id,
        title: `${release.channel} release tuple`,
        detail: release.artifact_id,
        status: "pass",
        time: release.minted_at,
        lane: laneName,
        kind: "release",
        facts: [
          { label: "Tuple", value: release.tuple_id, mono: true },
          { label: "Context", value: release.context },
          { label: "Channel", value: release.channel },
          { label: "Provenance", value: release.provenance },
          { label: "Artifact", value: release.artifact_id, mono: true },
          {
            label: "Image repository",
            value: release.image_repository ?? "unknown",
            mono: true,
          },
          {
            label: "Image digest",
            value: release.image_digest ?? "unknown",
            mono: true,
          },
          {
            label: "Deployment record",
            value: release.deployment_record_id ?? "unknown",
            mono: true,
          },
          {
            label: "Promotion record",
            value: release.promotion_record_id ?? "unknown",
            mono: true,
          },
          {
            label: "Promoted from",
            value: release.promoted_from_channel ?? "none",
          },
          ...Object.entries(release.repo_shas).map(([repo, sha]) => ({
            label: repo,
            value: sha,
            mono: true,
          })),
          { label: "Minted", value: formatTime(release.minted_at) },
        ],
      });
    }
    if (lane?.latest_deployment) {
      const deployment = lane.latest_deployment;
      const deploymentStatus = deployment.deploy.status ?? "unknown";
      const deploymentHealth = deployment.destination_health ?? {
        verified: false,
        status: "unknown",
        urls: [],
      };
      const deploymentHealthStatus = deploymentHealth.status ?? "unknown";
      rows.push({
        id: deployment.record_id,
        title: `${lane.instance} deployment`,
        detail: artifactFromLane(lane) || deployment.record_id,
        status: deploymentStatus,
        time: deployment.deploy.finished_at ?? lane.inventory?.updated_at ?? "",
        lane: laneName,
        kind: "deploy",
        facts: [
          { label: "Record", value: deployment.record_id, mono: true },
          {
            label: "Artifact",
            value: artifactFromLane(lane) || "unknown",
            mono: true,
          },
          {
            label: "Source ref",
            value: deployment.source_git_ref || "unknown",
            mono: true,
          },
          {
            label: "Target",
            value: deployment.deploy.target_name || "unknown",
          },
          {
            label: "Target type",
            value: deployment.deploy.target_type ?? "unknown",
          },
          {
            label: "Deploy mode",
            value: deployment.deploy.deploy_mode ?? "unknown",
          },
          {
            label: "Deployment id",
            value: deployment.deploy.deployment_id ?? "unknown",
            mono: true,
          },
          {
            label: "Deploy status",
            value: labelForStatus(deploymentStatus),
            status: deploymentStatus,
          },
          {
            label: "Health status",
            value: labelForStatus(deploymentHealthStatus),
            status: deploymentHealthStatus,
          },
          {
            label: "Health URLs",
            value: deploymentHealth.urls?.join(", ") || "none",
            mono: true,
          },
          {
            label: "Health verified",
            value: deploymentHealth.verified ? "yes" : "no",
          },
          {
            label: "Started",
            value: formatTime(deployment.deploy.started_at ?? ""),
          },
          {
            label: "Finished",
            value: formatTime(deployment.deploy.finished_at ?? ""),
          },
        ],
      });
    }
    if (lane?.latest_backup_gate) {
      const backup = lane.latest_backup_gate;
      const backupStatus = backup.status ?? "unknown";
      const backupEvidence = backup.evidence ?? {};
      rows.push({
        id: backup.record_id,
        title: `${lane.instance} backup gate`,
        detail: backup.source,
        status: backupStatus,
        time: backup.created_at,
        lane: laneName,
        kind: "backup",
        facts: [
          { label: "Record", value: backup.record_id, mono: true },
          { label: "Source", value: backup.source || "unknown" },
          { label: "Required", value: backup.required ? "yes" : "no" },
          {
            label: "Status",
            value: labelForStatus(backupStatus),
            status: backupStatus,
          },
          { label: "Created", value: formatTime(backup.created_at) },
          ...Object.entries(backupEvidence).map(([label, value]) => ({
            label,
            value,
            mono: true,
          })),
        ],
      });
    }
    if (lane?.latest_promotion) {
      const promotion = lane.latest_promotion;
      const promotionStatus = promotion.deploy.status ?? "unknown";
      const backupGate = promotion.backup_gate ?? {
        required: false,
        status: "unknown",
        evidence: {},
      };
      const destinationHealth = promotion.destination_health ?? {
        status: "unknown",
        urls: [],
      };
      rows.push({
        id: promotion.record_id,
        title: `${promotion.from_instance} to ${promotion.to_instance}`,
        detail: promotion.artifact_identity.artifact_id,
        status: promotionStatus,
        time: promotion.deploy.finished_at ?? "",
        lane: "prod",
        kind: "promote",
        facts: [
          { label: "Record", value: promotion.record_id, mono: true },
          {
            label: "Artifact",
            value: promotion.artifact_identity.artifact_id,
            mono: true,
          },
          { label: "From", value: promotion.from_instance },
          { label: "To", value: promotion.to_instance },
          {
            label: "Deployment record",
            value: promotion.deployment_record_id ?? "unknown",
            mono: true,
          },
          {
            label: "Backup record",
            value: promotion.backup_record_id ?? "unknown",
            mono: true,
          },
          {
            label: "Backup gate",
            value: labelForStatus(backupGate.status),
            status: backupGate.status,
          },
          {
            label: "Deploy target",
            value: promotion.deploy.target_name || "unknown",
          },
          {
            label: "Deploy status",
            value: labelForStatus(promotionStatus),
            status: promotionStatus,
          },
          {
            label: "Health status",
            value: labelForStatus(destinationHealth.status),
            status: destinationHealth.status,
          },
          {
            label: "Source health",
            value: labelForStatus(promotion.source_health?.status ?? "unknown"),
            status: promotion.source_health?.status ?? "unknown",
          },
          {
            label: "Source health URLs",
            value: promotion.source_health?.urls.join(", ") || "none",
            mono: true,
          },
          {
            label: "Health URLs",
            value: destinationHealth.urls?.join(", ") || "none",
            mono: true,
          },
          {
            label: "Health verified",
            value: promotion.destination_health.verified ? "yes" : "no",
          },
          {
            label: "Finished",
            value: formatTime(promotion.deploy.finished_at ?? ""),
          },
        ],
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
        {
          label: "Pull request",
          value: `${summary.preview.anchor_repo}#${summary.preview.anchor_pr_number}`,
        },
        { label: "URL", value: summary.preview.canonical_url, mono: true },
        { label: "State", value: summary.preview.state },
        {
          label: "Generation",
          value: generation?.generation_id ?? "none",
          mono: true,
        },
        {
          label: "Sequence",
          value: generation ? String(generation.sequence) : "none",
        },
        {
          label: "Artifact",
          value: generation?.artifact_id ?? "unknown",
          mono: true,
        },
        {
          label: "Deploy",
          value: labelForStatus(generation?.deploy_status ?? "unknown"),
          status: generation?.deploy_status ?? "unknown",
        },
        {
          label: "Verify",
          value: labelForStatus(generation?.verify_status ?? "unknown"),
          status: generation?.verify_status ?? "unknown",
        },
        {
          label: "Health",
          value: labelForStatus(generation?.overall_health_status ?? "unknown"),
          status: generation?.overall_health_status ?? "unknown",
        },
        { label: "Updated", value: formatTime(summary.preview.updated_at) },
        { label: "Finished", value: formatTime(generation?.finished_at ?? "") },
      ],
    });
  });
  return rows
    .sort((left, right) => right.time.localeCompare(left.time))
    .slice(0, 8);
}

function requiresProdBackup(actions: DriverActionDescriptor[]): boolean {
  return actions.some((action) => action.action_id === "prod_backup_gate");
}

function findDriverView(
  view: DriverContextView | null,
  driverId: string,
): DriverView | null {
  return view?.drivers.find((driver) => driver.driver_id === driverId) ?? null;
}

function fixtureLane({
  instance,
  artifact,
  deployStatus,
  healthStatus,
  backupStatus,
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
    finished_at:
      instance === "prod" ? "2026-04-28T14:40:00Z" : "2026-04-28T15:05:00Z",
  };
  const destinationHealth = {
    verified: healthStatus !== "unknown",
    urls: [`https://${instance}.verireel.example/api/health`],
    timeout_seconds: 60,
    status: healthStatus,
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
      deployment_record_id: `fixture-deployment-${instance}`,
      promotion_record_id:
        instance === "prod" ? "fixture-promotion-prod" : undefined,
      promoted_from_instance: instance === "prod" ? "testing" : undefined,
    },
    release_tuple: {
      tuple_id: `verireel-${instance}-20260428`,
      context: "verireel",
      channel: instance,
      artifact_id: artifact,
      repo_shas: {
        verireel: "6b3c9d7e8f901234567890abcdef1234567890ab",
      },
      image_repository: "ghcr.io/every/verireel",
      image_digest: `sha256:${instance === "prod" ? "prod" : "test"}fixture`,
      deployment_record_id: `fixture-deployment-${instance}`,
      promotion_record_id:
        instance === "prod" ? "fixture-promotion-prod" : undefined,
      promoted_from_channel: instance === "prod" ? "testing" : undefined,
      provenance: instance === "prod" ? "promotion" : "ship",
      minted_at: deploy.finished_at,
    },
    runtime_environment_records: [
      {
        scope: "context",
        context: "verireel",
        instance: "",
        env: {
          LAUNCHPLANE_PREVIEW_BASE_URL: "https://preview.verireel.example",
        },
        updated_at: deploy.finished_at,
        source_label: "fixture-context-env",
      },
      {
        scope: "instance",
        context: "verireel",
        instance,
        env: {
          CONTACT_EMAIL_MODE: "smtp",
          PUBLIC_TAWK_WIDGET_ID: "redacted-fixture",
        },
        updated_at: deploy.finished_at,
        source_label: "fixture-instance-env",
      },
    ],
    latest_deployment: {
      record_id: `fixture-deployment-${instance}`,
      artifact_identity: { artifact_id: artifact },
      context: "verireel",
      instance,
      source_git_ref: "6b3c9d7e8f901234567890abcdef1234567890ab",
      deploy,
      destination_health: destinationHealth,
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
          evidence: backupStatus === "pass" ? { snapshot: "fixture-prod" } : {},
        }
      : null,
    secret_bindings: [
      {
        binding_id: `fixture-secret-binding-${instance}`,
        secret_id: `fixture-secret-${instance}`,
        integration: "runtime_environment",
        binding_type: "env",
        binding_key: "SMTP_PASSWORD",
        context: "verireel",
        instance,
        status: "configured",
        created_at: deploy.finished_at,
        updated_at: deploy.finished_at,
      },
    ],
    provenance: {
      source_kind: "record",
      source_record_id: `fixture-deployment-${instance}`,
      recorded_at: deploy.finished_at,
      refreshed_at: deploy.finished_at,
      freshness_status: "verified",
      stale_after: "2026-04-28T15:35:00Z",
      detail: "Development fixture lane evidence.",
    },
  };
}

function safetyLabel(safety: Safety): string {
  return safety.replace("_", " ").toUpperCase();
}
