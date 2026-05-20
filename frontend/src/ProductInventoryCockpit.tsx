import { PanelsTopLeft } from "lucide-react";

import { EveryCodeQueue } from "./EveryCodeQueue";
import { MergeTrainControllerPanel } from "./MergeTrainControllerPanel";
import { MetricTile, PanelHead } from "./panel-ui";
import { StateBlock, SkeletonRows, StatusPill } from "./status-ui";
import { TrustBadge } from "./TrustBadge";

import type {
  EveryCodeWorkRequestRecord,
  ProductSiteOverview,
  MergeTrainControllerStatus,
  MergeTrainPolicyTarget,
  WorkGraphQueueItem,
} from "./types";
import {
  WorkGraphQueue,
  type WorkGraphFilter,
  type WorkGraphMode,
} from "./WorkGraphQueue";

export function ProductInventoryCockpit({
  products,
  selectedProduct,
  workRequests,
  workGraphItems,
  workGraphHiddenCount,
  workGraphError,
  workGraphFilter,
  workGraphMode,
  loading,
  onSelectProduct,
  onWorkGraphFilterChange,
  onWorkGraphModeChange,
  readMergeTrainStatus,
  readMergeTrainPolicyTargets,
}: {
  products: ProductSiteOverview[];
  selectedProduct: ProductSiteOverview | null;
  workRequests: EveryCodeWorkRequestRecord[];
  workGraphItems: WorkGraphQueueItem[];
  workGraphHiddenCount: number;
  workGraphError: string;
  workGraphFilter: WorkGraphFilter;
  workGraphMode: WorkGraphMode;
  loading: boolean;
  onSelectProduct: (product: ProductSiteOverview) => void;
  onWorkGraphFilterChange: (filter: WorkGraphFilter) => void;
  onWorkGraphModeChange: (mode: WorkGraphMode) => void;
  readMergeTrainStatus?: (
    repository: string,
    baseBranch: string,
    signal?: AbortSignal,
  ) => Promise<{ controller_status: MergeTrainControllerStatus }>;
  readMergeTrainPolicyTargets?: (
    signal?: AbortSignal,
  ) => Promise<{ targets: MergeTrainPolicyTarget[] }>;
}) {
  const activeWork = workRequests.filter((request) =>
    ["queued", "claimed", "running", "blocked"].includes(request.state),
  );
  const runningCount = workRequests.filter(
    (request) => request.state === "claimed" || request.state === "running",
  ).length;
  const blockedCount = workRequests.filter(
    (request) => request.state === "blocked",
  ).length;
  const queueStatus = blockedCount
    ? "blocked"
    : runningCount
      ? "pending"
      : activeWork.length
        ? "pending"
        : "pass";

  return (
    <section className="operator-cockpit" aria-busy={loading}>
      <div className="cockpit-summary">
        <PanelHead
          eyebrow="product inventory"
          title="Environment workbench"
          right={<StatusPill status={queueStatus} />}
        />
        <div className="cockpit-metrics">
          <MetricTile
            label="Products"
            value={String(products.length)}
            status={products.length ? "pass" : "unknown"}
          />
          <MetricTile
            label="Stable lanes"
            value={String(
              products.reduce(
                (total, product) => total + product.environments.length,
                0,
              ),
            )}
            status={
              products.some((product) => product.environments.length)
                ? "pass"
                : "unknown"
            }
          />
          <MetricTile
            label="Open work"
            value={String(activeWork.length)}
            status={queueStatus}
          />
        </div>
      </div>
      <div className="product-inventory-table" role="list">
        {loading && !products.length ? <SkeletonRows /> : null}
        {!loading && !products.length ? (
          <StateBlock
            icon={<PanelsTopLeft size={18} />}
            title="No product environment inventory"
          />
        ) : null}
        {products.map((product) => (
          <button
            className="product-inventory-row"
            type="button"
            key={product.product}
            data-selected={selectedProduct?.product === product.product}
            onClick={() => onSelectProduct(product)}
          >
            <span className="product-inventory-name">
              <strong>{product.display_name || product.product}</strong>
              <code>{product.repository || product.product}</code>
            </span>
            <span className="product-inventory-lanes">
              {product.environments.map((environment) => (
                <span
                  className={`lane-chip lane-chip-${environment.environment}`}
                  key={`${product.product}:${environment.environment}`}
                  title={environment.context}
                >
                  {environment.environment}
                </span>
              ))}
              {product.preview.enabled ? (
                <span className="lane-chip lane-chip-preview">
                  {product.preview.active_count} previews
                </span>
              ) : null}
            </span>
            <TrustBadge provenance={product.provenance} compact />
          </button>
        ))}
      </div>
      <WorkGraphQueue
        items={workGraphItems}
        hiddenCount={workGraphHiddenCount}
        error={workGraphError}
        filter={workGraphFilter}
        mode={workGraphMode}
        loading={loading}
        onFilterChange={onWorkGraphFilterChange}
        onModeChange={onWorkGraphModeChange}
      />
      <MergeTrainControllerPanel
        products={products}
        selectedProduct={selectedProduct}
        workGraphItems={workGraphItems}
        loading={loading}
        readStatus={readMergeTrainStatus}
        readPolicyTargets={readMergeTrainPolicyTargets}
      />
      <EveryCodeQueue requests={activeWork} loading={loading} />
    </section>
  );
}
