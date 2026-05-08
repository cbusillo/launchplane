import { Route, ShieldAlert } from "lucide-react";

import { formatTime } from "./format";
import { SkeletonRows, StateBlock, StatusIcon, StatusPill } from "./status-ui";
import type {
  Status,
  WorkGraphQueueItem,
  WorkGraphRecommendation,
  WorkGraphState,
} from "./types";

export type WorkGraphFilter = WorkGraphState | "all";
export type WorkGraphMode = WorkGraphRecommendation | "all";

export function WorkGraphQueue({
  items,
  hiddenCount,
  error,
  filter,
  mode,
  loading,
  onFilterChange,
  onModeChange,
}: {
  items: WorkGraphQueueItem[];
  hiddenCount: number;
  error: string;
  filter: WorkGraphFilter;
  mode: WorkGraphMode;
  loading: boolean;
  onFilterChange: (filter: WorkGraphFilter) => void;
  onModeChange: (mode: WorkGraphMode) => void;
}) {
  const filteredItems = items.filter((item) => {
    const stateMatches = filter === "all" || item.state === filter;
    const modeMatches = mode === "all" || item.recommendation === mode;
    return stateMatches && modeMatches;
  });
  const readyCount = items.filter((item) => item.state === "ready").length;
  const blockedCount = items.filter((item) => item.state === "blocked").length;
  const status: Status | string = error
    ? "fail"
    : blockedCount
      ? "blocked"
      : readyCount
        ? "pending"
        : items.length
          ? "unknown"
          : "pass";

  return (
    <div className="work-graph-queue">
      <div className="queue-head work-graph-head">
        <span>What next</span>
        <strong>{items.length ? `${items.length} ranked` : "clear"}</strong>
        <StatusPill status={status} />
      </div>
      <div className="work-graph-controls" aria-label="Work graph filters">
        <SegmentedControl<WorkGraphFilter>
          ariaLabel="Work state"
          value={filter}
          options={[
            { value: "all", label: "All" },
            { value: "ready", label: "Ready" },
            { value: "blocked", label: "Blocked" },
            { value: "waiting", label: "Waiting" },
          ]}
          onChange={onFilterChange}
        />
        <SegmentedControl<WorkGraphMode>
          ariaLabel="Recommendation"
          value={mode}
          options={[
            { value: "all", label: "Any" },
            { value: "quick_win", label: "Quick" },
            { value: "deep_work", label: "Deep" },
            { value: "switch_projects", label: "Switch" },
            { value: "attention_needed", label: "Needs" },
          ]}
          onChange={onModeChange}
        />
      </div>
      {error ? (
        <StateBlock icon={<ShieldAlert size={18} />} title={error} />
      ) : null}
      {loading && !items.length ? <SkeletonRows /> : null}
      {!loading && !error && !items.length ? (
        <StateBlock icon={<Route size={18} />} title="No ranked work yet" />
      ) : null}
      {!loading && !error && items.length && !filteredItems.length ? (
        <StateBlock
          icon={<Route size={18} />}
          title="No work matches filters"
        />
      ) : null}
      {filteredItems.slice(0, 5).map((item) => (
        <a
          className="work-graph-row"
          href={item.url}
          key={`${item.repository}:${item.number}`}
          target="_blank"
          rel="noreferrer"
        >
          <span className="work-graph-score">{item.score}</span>
          <span className="work-graph-content">
            <span className="work-graph-title">
              <strong>{item.title}</strong>
              <code>
                {item.repository}#{item.number}
              </code>
            </span>
            <span className="work-graph-meta">
              <span>{item.focus}</span>
              <span>{item.manager || "Unassigned"}</span>
              <span>{formatTime(item.updated_at)}</span>
              <span>
                {item.product_display_name ||
                  item.product ||
                  item.repo_classification}
              </span>
            </span>
            {item.finish_line ? (
              <span className="work-graph-finish">{item.finish_line}</span>
            ) : null}
            {item.next_action ? (
              <span className="work-graph-next-action">{item.next_action}</span>
            ) : null}
            <span className="work-graph-tags">
              <span
                className="status-pill"
                data-status={workGraphStateStatus(item.state)}
              >
                <StatusIcon status={workGraphStateStatus(item.state)} />
                {item.state}
              </span>
              <span className="reason-chip">
                {item.safe_to_start ? "safe to start" : "needs review"}
              </span>
              <span className="recommendation-chip">
                {recommendationLabel(item.recommendation)}
              </span>
              {item.reasons.slice(0, 2).map((reason) => (
                <span
                  className="reason-chip"
                  key={`${item.repository}:${item.number}:${reason.code}`}
                >
                  {reasonLabel(reason.code)}
                </span>
              ))}
            </span>
          </span>
        </a>
      ))}
      {hiddenCount ? (
        <code className="work-graph-hidden">
          {hiddenCount} hidden by rank or state
        </code>
      ) : null}
    </div>
  );
}

function SegmentedControl<T extends string>({
  ariaLabel,
  value,
  options,
  onChange,
}: {
  ariaLabel: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented-control" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          data-selected={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function workGraphStateStatus(state: WorkGraphState): Status | string {
  if (state === "ready") {
    return "pending";
  }
  if (state === "blocked") {
    return "blocked";
  }
  if (state === "waiting") {
    return "unknown";
  }
  return "pass";
}

function recommendationLabel(recommendation: WorkGraphRecommendation): string {
  return recommendation.replaceAll("_", " ");
}

function reasonLabel(code: string): string {
  return code.replaceAll("_", " ");
}
