import type {
  ApplyGenericWebProdPromotionData,
  ApplyProductConfigData,
  DispatchGenericWebProdPromotionWorkflowData,
  RankWorkGraphSnapshotData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  genericWebPromotion:
    "/v1/drivers/generic-web/prod-promotion" satisfies ApplyGenericWebProdPromotionData["url"],
  genericWebPromotionWorkflow:
    "/v1/drivers/generic-web/prod-promotion-workflow" satisfies DispatchGenericWebProdPromotionWorkflowData["url"],
  productConfigApply:
    "/v1/product-config/apply" satisfies ApplyProductConfigData["url"],
  workGraphRank:
    "/v1/work-graph/rank" satisfies RankWorkGraphSnapshotData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
