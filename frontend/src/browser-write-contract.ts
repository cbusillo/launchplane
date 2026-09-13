import type {
  ApplyProductEnvironmentConfigData,
  ApproveOrdinaryAgentOperationData,
  CancelOrdinaryAgentOperationData,
  RevokeOrdinaryAgentSessionData,
  DisconnectOrdinaryAgentPrincipalData,
  ApproveHumanPrivilegedOperationData,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  RankWorkGraphSnapshotData,
  RevokeHumanPrivilegedOperationData,
  PlanOrdinaryAgentDeliveryActivationData,
  PrepareAuthorizationCandidateData,
  PrepareOrdinaryAgentMergeTrainTargetData,
  WriteOwnerAcceptanceEventData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  ordinaryAgentApprove: "/v1/ordinary-agent-operations/{principal_id}/{operation_id}/approve" satisfies ApproveOrdinaryAgentOperationData["url"],
  ordinaryAgentCancel: "/v1/ordinary-agent-operations/{principal_id}/{operation_id}/cancel" satisfies CancelOrdinaryAgentOperationData["url"],
  ordinaryAgentRevokeSession: "/v1/ordinary-agent-sessions/{principal_id}/{session_id}/revoke" satisfies RevokeOrdinaryAgentSessionData["url"],
  ordinaryAgentDisconnect: "/v1/ordinary-agent-connections/{principal_id}/disconnect" satisfies DisconnectOrdinaryAgentPrincipalData["url"],
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
  ordinaryAgentDeliveryActivationPlan:
    "/v1/privileged-operations/ordinary-agent-delivery-activation/plans" satisfies PlanOrdinaryAgentDeliveryActivationData["url"],
  authorizationCandidatePrepare:
    "/v1/privileged-operations/authorization-candidates/prepare" satisfies PrepareAuthorizationCandidateData["url"],
  ordinaryMergeTrainTargetPrepare:
    "/v1/privileged-operations/merge-train-targets/prepare" satisfies PrepareOrdinaryAgentMergeTrainTargetData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
