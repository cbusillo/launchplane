import type {
  ApplyProductEnvironmentConfigData,
  ApproveHumanPrivilegedOperationData,
  BuildAuthzManagedSetRollbackProposalData,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  EvaluateEffectiveAccessData,
  PlanPrivilegedOperationData,
  RankWorkGraphSnapshotData,
  RevokeHumanPrivilegedOperationData,
  WriteOwnerAcceptanceEventData,
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
  ownerAcceptanceEvent:
    "/v1/owner-acceptance/events" satisfies WriteOwnerAcceptanceEventData["url"],
  privilegedOperationApprove:
    "/v1/privileged-operations/plans/{operation_id}/approve" satisfies ApproveHumanPrivilegedOperationData["url"],
  privilegedOperationPlan:
    "/v1/privileged-operations/plans" satisfies PlanPrivilegedOperationData["url"],
  privilegedOperationRevoke:
    "/v1/privileged-operations/plans/{operation_id}/revoke" satisfies RevokeHumanPrivilegedOperationData["url"],
  authzManagedSetRollbackProposal:
    "/v1/authz-policies/managed-rule-sets/rollback-proposal" satisfies BuildAuthzManagedSetRollbackProposalData["url"],
  authzEffectiveAccessEvaluate:
    "/v1/authz-diagnostics/effective-access/evaluate" satisfies EvaluateEffectiveAccessData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
