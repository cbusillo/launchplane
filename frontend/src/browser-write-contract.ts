import type {
  ApplyGenericWebProdPromotionData,
  ApplyProductEnvironmentConfigData,
  DispatchGenericWebProdPromotionWorkflowData,
  RankWorkGraphSnapshotData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  genericWebPromotion:
    "/v1/drivers/generic-web/prod-promotion" satisfies ApplyGenericWebProdPromotionData["url"],
  genericWebPromotionWorkflow:
    "/v1/drivers/generic-web/prod-promotion-workflow" satisfies DispatchGenericWebProdPromotionWorkflowData["url"],
  productEnvironmentConfigApply:
    "/v1/products/{product}/environments/{environment}/config/apply" satisfies ApplyProductEnvironmentConfigData["url"],
  workGraphRank:
    "/v1/work-graph/rank" satisfies RankWorkGraphSnapshotData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
