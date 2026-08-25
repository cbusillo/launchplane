import type {
  ApplyProductEnvironmentConfigData,
  ApproveHumanPrivilegedOperationData,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  EnrollAuthorizationRecoveryKeyData,
  RankWorkGraphSnapshotData,
  RevokeAuthorizationRecoveryKeyData,
  RevokeHumanPrivilegedOperationData,
  VerifyAuthorizationRecoveryKeyData,
  WriteOwnerAcceptanceEventData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  authorizationRecoveryEnroll:
    "/v1/authorization-recovery/keys/enroll" satisfies EnrollAuthorizationRecoveryKeyData["url"],
  authorizationRecoveryVerify:
    "/v1/authorization-recovery/keys/{key_id}/verify" satisfies VerifyAuthorizationRecoveryKeyData["url"],
  authorizationRecoveryRevoke:
    "/v1/authorization-recovery/keys/{key_id}/revoke" satisfies RevokeAuthorizationRecoveryKeyData["url"],
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
  privilegedOperationRevoke:
    "/v1/privileged-operations/plans/{operation_id}/revoke" satisfies RevokeHumanPrivilegedOperationData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
