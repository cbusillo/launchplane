import type {
  ApplyProductEnvironmentConfigData,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  RankWorkGraphSnapshotData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  productEnvironmentConfigApply:
    "/v1/products/{product}/environments/{environment}/config/apply" satisfies ApplyProductEnvironmentConfigData["url"],
  productPromotionDryRun:
    "/v1/products/{product}/environments/{environment}/promotion/dry-run" satisfies DryRunProductPromotionData["url"],
  productPromotionWorkflowDispatch:
    "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch" satisfies DispatchProductPromotionWorkflowData["url"],
  workGraphRank:
    "/v1/work-graph/rank" satisfies RankWorkGraphSnapshotData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
