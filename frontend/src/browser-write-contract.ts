import type {
  DecideBackupPolicyProposalData,
  ExecuteProductRetirementData,
  RebindBackupWorkerCredentialsData,
  ApplyProductionBackupAuthorityData,
  ApplyProductGenericWebTransitionData,
  ApplyProductDisplayNameData,
  ApplyProductHealthMonitoringData,
  ApplyProductWebsiteCleanupData,
  RestartLaneServiceData,
  ReadServiceDeliveryApplyReceiptData,
  ApplyProductExpectedConfigData,
  ApplyProductStableLaneRepairData,
  ApplyProductEnvironmentConfigData,
  ApplyProductImageRepositoryData,
  ApplyProductProductionUseData,
  ApplyProductOwnerData,
  ApproveHumanPrivilegedOperationData,
  DispatchProductPromotionWorkflowData,
  DryRunProductPromotionData,
  EnqueueOdooProdPromotionData,
  EnqueueOdooProdRollbackData,
  EnqueueProductionBackupGateData,
  RankWorkGraphSnapshotData,
  RevokeHumanPrivilegedOperationData,
  PrepareAuthorizationCandidateData,
  WriteProductReviewDecisionData,
  RetryProductReviewFeedbackData,
  WriteReleaseReviewDecisionData,
  OpenUrgentReleaseReviewData,
  SubmitOwnerSecretInputData,
  DeliverTestingLocalConsumerData,
  ConfigureLaunchplaneGithubDeliveryData,
  RetireServiceGithubTokensData,
} from "./generated/openapi.ts";

export const BROWSER_WRITE_ROUTES = {
  backupPolicyDecision: "/v1/production-backup-authority/proposals/{operation_id}/decision" satisfies DecideBackupPolicyProposalData["url"],
  localConsumerDelivery: "/v1/local-consumer-deliveries" satisfies DeliverTestingLocalConsumerData["url"],
  productRetirement: "/v1/product-retirement" satisfies ExecuteProductRetirementData["url"],
  backupWorkerBindings: "/v1/products/{product}/environments/prod/backup-worker-bindings/apply" satisfies RebindBackupWorkerCredentialsData["url"],
  productionBackupAuthorityApply: "/v1/production-backup-authority/apply" satisfies ApplyProductionBackupAuthorityData["url"],
  productGenericWebTransition: "/v1/product-profiles/{product}/generic-web-transition" satisfies ApplyProductGenericWebTransitionData["url"],
  productDisplayName: "/v1/product-profiles/{product}/display-name" satisfies ApplyProductDisplayNameData["url"],
  productHealthMonitoringApply: "/v1/product-profiles/health-monitoring/apply" satisfies ApplyProductHealthMonitoringData["url"],
  productWebsiteCleanup: "/v1/product-profiles/{product}/website-cleanup" satisfies ApplyProductWebsiteCleanupData["url"],
  laneServiceRestart: "/v1/drivers/odoo/service-restart" satisfies RestartLaneServiceData["url"],
  serviceDeliveryReceiptRead: "/v1/service/delivery/apply-receipt" satisfies ReadServiceDeliveryApplyReceiptData["url"],
  productExpectedConfigApply: "/v1/product-profiles/expected-config/apply" satisfies ApplyProductExpectedConfigData["url"],
  productStableLaneRepairApply: "/v1/product-profiles/stable-lane-repair/apply" satisfies ApplyProductStableLaneRepairData["url"],
  serviceGitHubDeliveryConfigure: "/v1/service/github-delivery/configuration" satisfies ConfigureLaunchplaneGithubDeliveryData["url"],
  serviceGitHubTokenRetire: "/v1/service/github-delivery/token-retirement" satisfies RetireServiceGithubTokensData["url"],
  ownerSecretSubmit: "/v1/owner-secret-inputs/submit" satisfies SubmitOwnerSecretInputData["url"],
  releaseReviewDecision: "/v1/release-review/decisions" satisfies WriteReleaseReviewDecisionData["url"],
  releaseReviewUrgentOpening: "/v1/release-review/urgent-openings" satisfies OpenUrgentReleaseReviewData["url"],
  productEnvironmentConfigApply:
    "/v1/products/{product}/environments/{environment}/config/apply" satisfies ApplyProductEnvironmentConfigData["url"],
  productImageRepositoryApply: "/v1/product-profiles/{product}/image-repository" satisfies ApplyProductImageRepositoryData["url"],
  productProductionUseApply: "/v1/product-profiles/{product}/production-use" satisfies ApplyProductProductionUseData["url"],
  productOwnerApply:
    "/v1/product-profiles/{product}/owner" satisfies ApplyProductOwnerData["url"],
  productPromotionDryRun:
    "/v1/products/{product}/environments/{environment}/promotion/dry-run" satisfies DryRunProductPromotionData["url"],
  productPromotionWorkflowDispatch:
    "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch" satisfies DispatchProductPromotionWorkflowData["url"],
  productionBackupGateEnqueue:
    "/v1/production-backup-gates" satisfies EnqueueProductionBackupGateData["url"],
  odooProdPromotionEnqueue:
    "/v1/odoo-prod-promotions" satisfies EnqueueOdooProdPromotionData["url"],
  odooProdRollbackEnqueue:
    "/v1/odoo-prod-rollbacks" satisfies EnqueueOdooProdRollbackData["url"],
  workGraphRank:
    "/v1/work-graph/rank" satisfies RankWorkGraphSnapshotData["url"],
  productReviewDecision:
    "/v1/product-review/decisions" satisfies WriteProductReviewDecisionData["url"],
  productReviewFeedbackRetry:
    "/v1/product-review/feedback/retry" satisfies RetryProductReviewFeedbackData["url"],
  privilegedOperationApprove:
    "/v1/privileged-operations/plans/{operation_id}/approve" satisfies ApproveHumanPrivilegedOperationData["url"],
  privilegedOperationRevoke:
    "/v1/privileged-operations/plans/{operation_id}/revoke" satisfies RevokeHumanPrivilegedOperationData["url"],
  authorizationCandidatePrepare:
    "/v1/privileged-operations/authorization-candidates/prepare" satisfies PrepareAuthorizationCandidateData["url"],
} as const;

export type BrowserWriteRoute =
  (typeof BROWSER_WRITE_ROUTES)[keyof typeof BROWSER_WRITE_ROUTES];
