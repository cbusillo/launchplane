import type {
  ProductActionAvailability,
  ProductEnvironmentDetail,
  ReadProductOperationalReadinessData,
} from "./generated/openapi.ts";

export type ProductOperationalReadinessRequest = Pick<
  ReadProductOperationalReadinessData,
  "path" | "query"
>;

export function inspectableReadinessActions(
  actions: ProductActionAvailability[],
): ProductActionAvailability[] {
  const counts = new Map<string, number>();
  for (const action of actions) {
    const actionAuthzActions = new Set(
      [action.authz_action, ...action.alternate_authz_actions]
        .map((authzAction) => authzAction.trim())
        .filter(Boolean),
    );
    for (const authzAction of actionAuthzActions) {
      counts.set(authzAction, (counts.get(authzAction) ?? 0) + 1);
    }
  }
  return actions.filter((action) => {
    const authzAction = action.authz_action.trim();
    return authzAction !== "" && counts.get(authzAction) === 1;
  });
}

export function buildOperationalReadinessRequest(
  detail: ProductEnvironmentDetail,
  action: ProductActionAvailability,
): ProductOperationalReadinessRequest {
  const persistedArtifactId =
    detail.target.artifact_manifest?.artifact_id.trim() ?? "";
  const currentArtifactId =
    detail.target.expected_runtime_identity?.artifact_id.trim() ?? "";
  const artifactId = persistedArtifactId || currentArtifactId;
  return {
    path: {
      product: detail.product,
      context: detail.context,
      instance: detail.environment,
    },
    query: {
      action: action.authz_action.trim(),
      ...(artifactId ? { artifact_id: artifactId } : {}),
      ...(currentArtifactId
        ? {
            expected_current_artifact_id: currentArtifactId,
          }
        : {}),
    },
  };
}
